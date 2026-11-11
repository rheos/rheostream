# ADR-0019: Decisions retain target snapshots for orphan proposals
Status: accepted
Date: 2026-11-08

## Context

A parser or normalizer change can change a line's natural key, leaving a decision pointing to a line that no longer resolves. Silently dropping the decision or automatically attaching it to a similar line would conceal that change.

## Decision

A line-keyed decision records a `decision_target` snapshot for each target, including its account, amount, currency and posting date. The natural key determines whether the decision applies; snapshot similarity never does. When a decision is orphaned, the snapshot supplies facts for possible re-key candidates, which are offered for owner review rather than automatically attached.

## Consequences

An orphan remains visible when no candidate can be found, including when an older decision has no snapshot. Candidate matching can require review and does not itself restore the decision. ADR-0010 describes how line references survive ordinary overlap and rebuilds.
