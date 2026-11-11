# ADR-0020: Card overdue is the unpaid threshold
Status: accepted
Date: 2026-11-08

## Context

A card statement's full balance can include an amount carried beyond the due date. Treating that entire balance as overdue would overstate what had to be paid and generate an unwarranted reminder.

## Decision

A card obligation records both its statement balance and its payment threshold: the printed minimum, or the full balance when no minimum is printed. Payments fill the threshold first. After the due date only the unpaid threshold is overdue; the remainder is carried balance. For other obligation kinds the remaining amount is the threshold.

## Consequences

Carried balance alone is neither overdue nor a reason for a reminder or nudge. A later statement incorporates the carried balance in its own figures, avoiding double counting. Current reminders reconcile the unpaid threshold under ADR-0014; this record does not enable the sink or establish first-live operation.
