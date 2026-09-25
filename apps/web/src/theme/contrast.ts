const OPAQUE_HEX = /^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/;

function channel(hex: string): number {
  const c = Number.parseInt(hex, 16) / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** WCAG 2 relative luminance of an opaque `#rrggbb` color. */
export function relativeLuminance(color: string): number {
  const match = OPAQUE_HEX.exec(color);
  if (match === null) {
    // A translucent color has no contrast ratio until it is composited onto a
    // background, so refuse it rather than guess.
    throw new Error(`contrast needs an opaque #rrggbb color, got ${color}`);
  }
  return 0.2126 * channel(match[1]) + 0.7152 * channel(match[2]) + 0.0722 * channel(match[3]);
}

/** WCAG 2 contrast ratio between two opaque colors, from 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const [light, dark] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}
