"use client";

import { useState } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { ActionForm } from "./action-form";

const PRIORITIES = [
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "normal", label: "Normal" },
  { value: "low", label: "Low" },
];

function requestKey(): string {
  return crypto.randomUUID();
}

/** One client-owned request identity survives an unavailable response and retry. */
export function HandoffRequestForm({
  submit,
  opportunityRef,
  revision,
  title,
}: {
  submit: NonNullable<ShellApi["submit"]>;
  opportunityRef: string;
  revision: unknown;
  title: string;
}) {
  const [idempotencyKey] = useState(requestKey);
  return (
    <ActionForm
      submit={submit}
      operation="leads.handoff.request"
      base={{
        ref: opportunityRef,
        revision,
        purpose: "internal_analysis",
        destination_ref: "current",
        idempotency_key: idempotencyKey,
      }}
      fields={[
        {
          name: "title",
          label: "Title",
          value: title,
          required: true,
          maxLength: 500,
        },
        {
          name: "body",
          label: "Notes",
          type: "textarea",
          maxLength: 20000,
        },
        {
          name: "priority",
          label: "Priority",
          type: "select",
          value: "normal",
          required: true,
          options: PRIORITIES,
        },
        {
          name: "area",
          label: "Area key",
          maxLength: 40,
        },
        {
          name: "due_on",
          label: "Due date",
          type: "date",
        },
      ]}
      button="Hand off to Current"
      okCopy="Handoff request staged for approval."
      refusalCopy={{
        qualification_required:
          "Record a current qualification before handing this opportunity off.",
        idempotency_key_reused:
          "This request changed after it was sent. Reload before trying again.",
        record_stale:
          "This opportunity changed after the page loaded. Reload before trying again.",
      }}
    />
  );
}
