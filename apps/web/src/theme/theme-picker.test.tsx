import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./theme-action", () => ({ selectTheme: async () => {} }));

const { ThemePicker } = await import("./theme-picker");

describe("ThemePicker (#408)", () => {
  it("renders a native Apply button for the page before scripts run", () => {
    const html = renderToStaticMarkup(<ThemePicker current="greenstream-dark" />);
    expect(html).toContain('<button type="submit">Apply</button>');
    expect(html).not.toContain("<noscript>");
  });

  it("offers the mode only for Novadiem", () => {
    expect(renderToStaticMarkup(<ThemePicker current="greenstream-dark" />)).not.toContain('name="scheme"');
    expect(renderToStaticMarkup(<ThemePicker current="novadiem-light" />)).toMatch(
      /name="scheme"[^>]*>.*<option value="light" selected="">/,
    );
  });
});
