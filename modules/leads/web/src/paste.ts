/** Conservative suggestions only. Original text never becomes an instruction. */
export const PASTE_LIMIT = 16000;
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
  const emails =
    source.match(
      /[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+/gi,
    ) ?? [];
  return {
    subject: (labeled("subject") || "Pasted inquiry").slice(0, 512),
    "person.name": unique(names).slice(0, 512),
    "person.email": unique(emails).slice(0, 512),
    "person.phone": labeled("phone|tel|telephone").slice(0, 128),
    "organization.name": labeled("organization|company").slice(0, 512),
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
