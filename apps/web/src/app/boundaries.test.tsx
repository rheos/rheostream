import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import ErrorBoundary from "./error";
import NotFound from "./not-found";

/** The two file-convention boundaries render a real page, never a blank one. */

describe("not-found boundary", () => {
  it("renders a heading and a message", () => {
    const html = renderToStaticMarkup(<NotFound />);
    expect(html).toContain("<h1");
    expect(html).toContain("Page not found");
  });
});

describe("error boundary", () => {
  const retry = () => undefined;

  it("offers a retry and shows the digest, never the message", () => {
    const error = Object.assign(new Error("synthetic secret detail"), { digest: "d-123" });
    const html = renderToStaticMarkup(<ErrorBoundary error={error} retry={retry} />);
    expect(html).toContain("Something went wrong");
    expect(html).toContain("<code>d-123</code>");
    expect(html).toContain(">Try again</button>");
    expect(html).not.toContain("synthetic secret detail");
  });

  it("omits the reference line when there is no digest", () => {
    const html = renderToStaticMarkup(<ErrorBoundary error={new Error("x")} retry={retry} />);
    expect(html).not.toContain("Reference");
  });
});
