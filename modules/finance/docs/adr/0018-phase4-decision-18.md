# ADR-0018: Payments target the obligation's issue window
Status: accepted
Date: 2026-11-08

## Context

The same payment can appear on both sides of a transfer and in notices. Equal amounts and dates can also describe separate payments. A later bill or statement may already include earlier payments, so assigning an old payment to it can count that money twice.

## Decision

The applied pairing graph establishes when a debit and card credit are one payment; amount and date alone never merge payments (ADR-0012). A notice attaches to at most one payment. A notice outside the evidence window with an equal payment of the same payee and currency in the longer hold window is held as `confirm_only`, awaiting a decision rather than creating another active payment.

Automatic card application targets the statement with the latest `statement_date` on or before `paid_on`, without early-date slack. Other obligations are candidates when `issued_on` is on or before `paid_on` plus `finance.payments.early_days`; a missing issue date imposes no bound. Superseded or presumed-paid obligations can retain applications in their own windows. Explicit owner application decisions can override the automatic window.

An active payment with no eligible target remains an `unapplied_credit` with reason `no_target`, including when only the payee is known and no obligation exists. It does not automatically roll forward to a later-issued obligation outside the bound. An eligible obligation loaded later may take it.

## Consequences

Historical applications remain traceable without reducing a newer statement that already accounts for them. Ambiguous matches and held notices need owner confirmation. The early-date allowance is a configured rule, not a claim of fresh archive validation.
