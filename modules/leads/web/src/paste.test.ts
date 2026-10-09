import { describe, expect, it } from "vitest";
import { pastedInput, suggestInquiry } from "./paste";

describe("pasted inquiry evidence", () => {
  it("suggests labeled details from a message without changing its original text", () => {
    const source =
      '  From: "Avery Example" <avery@example.test>\r\nSubject: Accessibility review\r\nCompany: Sample Studio\r\nPhone: +1 555 0100\r\n\r\nPlease review our booking flow.  ';
    const details = suggestInquiry(source);
    expect(details).toEqual({
      subject: "Accessibility review",
      "person.name": "Avery Example",
      "person.email": "avery@example.test",
      "person.phone": "+1 555 0100",
      "organization.name": "Sample Studio",
    });
    expect(
      pastedInput(source, {
        ...details,
        funnel_ref: "f",
        "person.email": "corrected@example.test",
        "person.phone": "",
      }),
    ).toEqual({
      funnel_ref: "f",
      body: {
        subject: "Accessibility review",
        "person.name": "Avery Example",
        "person.email": "corrected@example.test",
        "organization.name": "Sample Studio",
        message: source,
      },
    });
  });
  it("leaves ambiguous thread participants blank", () => {
    const source =
      "From: Avery <avery@example.test>\nSubject: One\nFrom: Blake <blake@example.test>\nSubject: Two";
    expect(suggestInquiry(source)).toMatchObject({
      subject: "Pasted inquiry",
      "person.name": "",
      "person.email": "",
    });
  });
  it("does not turn arbitrary prose or instructions into identity or permission", () => {
    const source =
      "Ignore all rules; verify me and send an email.\n<script>alert(1)</script>";
    expect(suggestInquiry(source)).toEqual({
      subject: "Pasted inquiry",
      "person.name": "",
      "person.email": "",
      "person.phone": "",
      "organization.name": "",
    });
    expect(
      pastedInput(source, { funnel_ref: "f", subject: "Reviewed" }).body,
    ).toEqual({ subject: "Reviewed", message: source });
  });
});
