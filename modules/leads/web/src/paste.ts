/** Conservative suggestions only. Original text never becomes an instruction. */
export const PASTE_LIMIT = 16000;

const EMAIL = /[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+/gi;
// Grouped numbers only: separators between the groups keep order numbers, dates
// and amounts like "$8-12k" from reading as phones.
const PHONE =
  /(?<![\w+])(?:\+1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\w)|(?<![\w+])\+(?:[02-9]\d{0,2})(?:[\s.-]\d{1,4}){2,5}(?!\w)/g;
const SIGN_OFF =
  /^(?:thanks|thank you|many thanks|best|best regards|kind regards|regards|warm regards|cheers|sincerely|all the best|talk soon)[,!.]?$/i;
const NAME_WORD = "\\p{Lu}[\\p{L}'’.-]*";
const FULL_NAME = new RegExp(`^${NAME_WORD}(?: ${NAME_WORD}){1,3}$`, "u");
const ONE_NAME = new RegExp(`^${NAME_WORD}$`, "u");

/** The digits a phone number dials, so one number in two formats counts once. */
function dialed(phone: string): string {
  const digits = phone.replace(/\D/g, "");
  return digits.length === 11 && digits.startsWith("1")
    ? digits.slice(1)
    : digits;
}

const GREETING = /^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b/i;

/** A name from the closing contact block: the line after a sign-off, or the block's
 * first line when it is name-shaped. Only a block that also carries an email or a
 * phone counts as a signature, so a closing sentence never becomes a name; a block
 * that is the whole message needs a sign-off, so its opening line is never used.
 * Two different candidates in the block leave the name blank. */
function signatureName(lines: string[]): string {
  let end = lines.length;
  while (end > 0 && !lines[end - 1]?.trim()) end -= 1;
  let start = end;
  while (start > 0 && lines[start - 1]?.trim()) start -= 1;
  const block = lines.slice(start, end).map((line) => line.trim());
  const contact = block.some((line) => {
    EMAIL.lastIndex = 0;
    PHONE.lastIndex = 0;
    return EMAIL.test(line) || PHONE.test(line);
  });
  if (!contact || block.length === 0) return "";
  const isName = (line: string, single: boolean) =>
    !GREETING.test(line) &&
    line.length <= 60 &&
    (FULL_NAME.test(line) || (single && ONE_NAME.test(line)));
  const signOff = block.findIndex((line) => SIGN_OFF.test(line));
  const candidate =
    signOff >= 0
      ? (block[signOff + 1] ?? "")
      : start > 0
        ? (block[0] ?? "")
        : "";
  if (!isName(candidate, signOff >= 0)) return "";
  const others = block.filter(
    (line) => line !== candidate && isName(line, false),
  );
  return others.length === 0 ? candidate : "";
}

export function suggestInquiry(source: string): Record<string, string> {
  const lines = source.split(/\r?\n/);
  const unique = (values: string[]) => {
    const nonempty = [...new Set(values.map((v) => v.trim()).filter(Boolean))];
    return nonempty.length === 1 ? (nonempty[0] ?? "") : "";
  };
  const valuesFor = (label: string) =>
    lines.flatMap((line) => {
      const match = line.match(new RegExp(`^\\s*(?:${label}):\\s*(.+)$`, "i"));
      return match?.[1] ? [match[1]] : [];
    });
  const labeled = (label: string) => unique(valuesFor(label));
  const names = [
    ...valuesFor("name|contact name"),
    ...valuesFor("from").flatMap((from) => {
      const sender = from.match(/^([^<>]+)\s*<[^<>]+>$/);
      return sender?.[1] ? [sender[1].trim().replace(/^"(.*)"$/, "$1")] : [];
    }),
  ];
  const emails = source.match(EMAIL) ?? [];
  // One number written two ways is one number, labeled or not; labels win.
  const samePhone = (found: string[]) => {
    const kept = found.map((v) => v.trim()).filter(Boolean);
    return new Set(kept.map(dialed)).size === 1 ? (kept[0] ?? "") : "";
  };
  const phoneLabels = valuesFor("phone|tel|telephone");
  const phone =
    phoneLabels.length > 0
      ? samePhone(phoneLabels)
      : samePhone(source.match(PHONE) ?? []);
  const name = names.length > 0 ? unique(names) : signatureName(lines);
  const organization = labeled("organization|company");
  // A conflicting Subject: label stays neutral rather than falling back to identity.
  const subject =
    valuesFor("subject").length > 0
      ? labeled("subject") || "Pasted inquiry"
      : organization || name
        ? `Inquiry from ${organization || name}`
        : "Pasted inquiry";
  return {
    subject: subject.slice(0, 512),
    "person.name": name.slice(0, 512),
    "person.email": unique(emails).slice(0, 512),
    "person.phone": phone.slice(0, 128),
    "organization.name": organization.slice(0, 512),
  };
}

export function pastedInput(source: string, values: Record<string, string>) {
  const { funnel_ref, ...details } = values;
  return {
    funnel_ref,
    body: {
      ...Object.fromEntries(
        Object.entries(details).filter(([, value]) => value.trim()),
      ),
      message: source,
    },
  };
}
