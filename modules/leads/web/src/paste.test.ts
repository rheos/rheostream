import { describe, expect, it } from "vitest";
import { pastedInput, suggestInquiry } from "./paste";

describe("pasted inquiry evidence", () => {
  it("leaves conflicting explicit names and sender names blank", () => {
    expect(
      suggestInquiry("Name: Avery\nFrom: Blake <blake@example.test>")[
        "person.name"
      ],
    ).toBe("");
    expect(
      suggestInquiry(
        "Name: Avery\nName: Blake\nFrom: Avery <avery@example.test>",
      )["person.name"],
    ).toBe("");
  });
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

  it("suggests a signature name, an unlabeled phone and a title from a plain email", () => {
    const source =
      "Hi Robin,\n\nWe keep double-booking crews. Budget is $8-12k.\n\nDana Whitfield\ndana.whitfield@example.com\n250-555-0142\n";
    expect(suggestInquiry(source)).toEqual({
      subject: "Inquiry from Dana Whitfield",
      "person.name": "Dana Whitfield",
      "person.email": "dana.whitfield@example.com",
      "person.phone": "250-555-0142",
      "organization.name": "",
    });
  });
  it("takes the name after a sign-off, and never the greeting", () => {
    const source =
      "Hi Robin,\nCan we talk about a quote?\n\nThanks,\nDana\n(250) 555-0142";
    expect(suggestInquiry(source)).toMatchObject({
      "person.name": "Dana",
      "person.phone": "(250) 555-0142",
    });
  });
  it("prefers the organization for the title and keeps an explicit subject", () => {
    expect(
      suggestInquiry("Company: Sample Studio\nCall me on 555-555-0100")
        .subject,
    ).toBe("Inquiry from Sample Studio");
    expect(
      suggestInquiry("Subject: Booking app\n\nAvery Example\n555-555-0100")
        .subject,
    ).toBe("Booking app");
  });
  it("counts one number in two formats once and leaves two numbers blank", () => {
    expect(
      suggestInquiry("Call +1 250 555 0142 or 250.555.0142")["person.phone"],
    ).toBe("+1 250 555 0142");
    expect(
      suggestInquiry("Office 250-555-0142, cell 250-555-0199")["person.phone"],
    ).toBe("");
  });
  it("does not read amounts, dates or order numbers as phones", () => {
    expect(
      suggestInquiry(
        "Budget $8-12k by 2026-03-01, order 12345678901, invoice 555-0142",
      )["person.phone"],
    ).toBe("");
  });
  it("needs contact details in the closing block before suggesting a name", () => {
    expect(
      suggestInquiry(
        "dana@example.com wrote earlier.\n\nLooking Forward To It",
      )["person.name"],
    ).toBe("");
    expect(
      suggestInquiry(
        "Name: Avery\n\nDana Whitfield\ndana.whitfield@example.com",
      )["person.name"],
    ).toBe("Avery");
  });
});
