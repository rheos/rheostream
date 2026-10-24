import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./time-zone-action", () => ({ detectTimeZone: async () => {}, selectTimeZone: async () => {} }));

const { TimeZonePicker } = await import("./time-zone-picker");

const ZONES = ["UTC", "America/Vancouver", "Europe/London"];

describe("TimeZonePicker (#465)", () => {
  it("names the detected zone on Automatic and keeps an Apply button for no-script pages", () => {
    const html = renderToStaticMarkup(
      <TimeZonePicker current={{ zone: "America/Vancouver", chosen: false }} zones={ZONES} />,
    );
    expect(html).toContain('<option value="auto" selected="">Automatic (America/Vancouver)</option>');
    expect(html).toContain('<button type="submit">Apply</button>');
  });

  it("selects a chosen zone", () => {
    const html = renderToStaticMarkup(
      <TimeZonePicker current={{ zone: "Europe/London", chosen: true }} zones={ZONES} />,
    );
    expect(html).toContain('<option value="Europe/London" selected="">Europe/London</option>');
  });
});
