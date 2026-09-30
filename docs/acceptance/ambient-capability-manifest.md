# Supported-client ambient capability manifest

## What this is

Which clients can produce automatic memory, what each one needs before it records, what it
refuses, what it reports as a gap, and what it does not promise. There is one row per path:
the Rheo runtime producer, and the local Claude Code bridge under each of its two clients.
The mechanisms are described in
[runtime and MCP](../architecture/runtime-and-mcp.md#automatic-memorys-two-producers) and
[the local Claude Code bridge](../architecture/runtime-and-mcp.md#the-local-claude-code-bridge).

Every `pytest:` id below names a test function that exists in this tree.
`tests/test_ambient_manifest.py` checks that mechanically by parsing each named file, without
running anything. It does not check that a test proves what its row claims; that is signed
by the row, as in the phase matrices.

"Operative" means the mechanism is built and proven by the fixtures below. For the two local
rows it is **operative, pending CP-A/CP-B for real use**: the tests prove the mechanism on
synthetic transcripts, settings files and tokens. The real-session proof waits for the two
separately authorized steps, gate A (installing the hook in a real enrolled directory) and
gate B (setting `automatic_memory.extraction.provider = "claude_cli"`).

## Row: `rheo_runtime`

**Client:** runs dispatched through a Rheo runtime adapter (`producer_kind = "rheo_runtime"`).

**State:** operative. Production records nothing until `automatic_memory.enabled` is true at
the operator and workspace level and an extraction provider resolves.

**Enrollment requirement:** none. A turn is recorded only when the six recording conditions
hold, and only as the run's own account and bound purpose; the evidence row's authority is
the run.

**Fails closed:** a model-held token (`mcp` or `runtime`) is never a speaker; a unit whose
producer kind, speaker or pending row does not match is refused; a speaker whose membership
was revoked is refused at claim.

**Gap:** a pending turn older than `automatic_memory.max_pending_hours` settles
`gap/expired_pending` with no receipt.

**Not guaranteed:** no hook-finality or spawned-process durability (R4 is stated for the
local bridge, and the runtime producer claims no more). R10 and the one-directory limit do
not apply: the run token lives for one run and is deleted when it ends.

**Refusal:**
- `pytest:tests/postgres/test_evidence_authority.py::test_a_unit_of_another_producer_kind_refuses_producer_mismatch`
- `pytest:tests/postgres/test_evidence_authority.py::test_a_speaker_whose_membership_was_revoked_refuses_membership_revoked`
- `pytest:tests/postgres/test_evidence_authority.py::test_a_unit_naming_another_principal_refuses_speaker_mismatch`
- `pytest:tests/postgres/test_evidence_authority.py::test_a_missing_row_refuses_source_unavailable`
- `pytest:tests/postgres/test_runtime_evidence_hook.py::test_condition_4_a_model_held_token_records_nothing`

**Replay:**
- `pytest:tests/postgres/test_automatic_memory_acceptance.py::test_ac4a_an_in_transaction_replay_answers_the_same_ref_and_writes_once`
- `pytest:tests/postgres/test_automatic_memory_acceptance.py::test_ac4b_a_redelivery_after_commit_claims_nothing_and_changes_nothing`
- `pytest:tests/postgres/test_automatic_memory_acceptance.py::test_ac4c_a_restart_before_commit_leaves_the_row_pending_and_accepts_once`
- `pytest:tests/postgres/test_runtime_evidence_hook.py::test_a_turn_already_held_is_not_recorded_twice`

**Gap proof:**
- `pytest:tests/postgres/test_automatic_memory_limits.py::test_ac5_a_unit_past_max_pending_hours_is_an_expired_gap_with_no_receipt`

## Row: `claude_code_local` via Claude Code CLI

**Client:** Claude Code CLI sessions started in the enrolled directory; transcript
`entrypoint = "cli"`.

**State:** operative, pending CP-A/CP-B for real use.

**Enrollment requirement:** an active `core.evidence_enrollment` row for this machine and
directory fingerprint pair, a bridge token (`cli` kind, snapshot exactly
`core.evidence.ingest`) bound to it and stored by `rheo-bridge set-token`, and the hook
installed in the enrolled directory's `.claude/settings.local.json`.

**Fails closed:** with no hook installed nothing is spooled or sent; a transcript path outside
the enrolled directory's slug is refused and never read; a token no active enrollment holds,
a revoked enrollment, a wrong machine or project fingerprint, or a moved account or purpose
is refused before any row is written; the bridge token is refused every other operation;
after a rotate the old token is refused; with recording off every key is deferred and
nothing is recorded; at claim, `LocalEvidenceAuthority` refuses with one of its six words.

**Gap:** `source_truncated` when a transcript shrank, was replaced or vanished under the
cursor; `expired_pending` when a line is older than the local pending limit (24 hours by
default) by the time it can be sent. Both are content-free rows.

**Not guaranteed:**
- R4: no hook-finality guarantee. If every hook call for a session fails, there is no
  locator and nothing to gap-report.
- R10: token rotation is an operator step. A lapsed token stops the worker (exit 2) with its
  state intact until `rheo evidence rotate` is piped into `rheo-bridge set-token`.
- One enrolled directory per bridge home.
- The slug is lossy: every character outside `[A-Za-z0-9]` maps to `-`, so `/work/a-b`,
  `/work/a_b` and `/work/a/b` share one slug. The worker reads only paths the enrolled
  directory's own hook wrote to the spool and never crawls the projects root, so a session
  from a colliding directory is read only if that directory also runs the enrolled hook.

**Refusal:**
- `pytest:tests/postgres/test_local_authority.py::test_a_unit_of_another_producer_kind_refuses_producer_mismatch`
- `pytest:tests/postgres/test_local_authority.py::test_a_unit_naming_another_principal_refuses_speaker_mismatch`
- `pytest:tests/postgres/test_local_authority.py::test_a_revoked_enrollment_refuses_enrollment_inactive`
- `pytest:tests/postgres/test_local_authority.py::test_an_enrollment_of_another_account_refuses_enrollment_mismatch`
- `pytest:tests/postgres/test_local_authority.py::test_a_speaker_whose_membership_was_revoked_refuses_membership_revoked`
- `pytest:tests/postgres/test_local_authority.py::test_the_refusal_vocabulary_is_exactly_the_six_words`
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_token_no_enrollment_holds_is_refused`
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_live_token_under_a_revoked_enrollment_is_refused`
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_wrong_fingerprint_is_refused`
- `pytest:tests/postgres/test_evidence_ingest.py::test_the_bridge_token_is_refused_every_other_operation`
- `pytest:tests/postgres/test_evidence_ingest.py::test_after_a_rotate_the_new_token_ingests_and_the_old_one_is_refused`

**Replay:**
- `pytest:tests/postgres/test_bridge_ingest_roundtrip.py::test_the_worker_drains_a_session_into_one_memory_per_turn`
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_replayed_batch_answers_the_same_and_writes_nothing_new`
- `pytest:tests/test_bridge_worker.py::test_a_crash_before_the_acknowledgement_moves_nothing_and_resends`

**Fails-closed proof:**
- `pytest:tests/test_bridge_install.py::test_no_hook_installed_means_nothing_is_spooled_or_sent`
- `pytest:tests/test_bridge_worker.py::test_refused_paths_make_no_request_through_the_drain`
- `pytest:tests/postgres/test_evidence_ingest.py::test_an_inert_deployment_defers_every_key`

**Gap proof:**
- `pytest:tests/postgres/test_evidence_ingest.py::test_gaps_land_in_gapped_as_content_free_rows`
- `pytest:tests/test_bridge_worker.py::test_a_deleted_transcript_yields_the_gap_and_a_pending_key`
- `pytest:tests/test_bridge_worker.py::test_an_expired_line_becomes_a_textless_gap_not_a_record`
- `pytest:tests/test_bridge_transcript.py::test_slug_keeps_letters_and_digits_and_replaces_everything_else`

**Entrypoint:**
- `pytest:tests/test_bridge_transcript.py::test_entrypoint`
- `pytest:tests/test_bridge_worker.py::test_accepted_counts_are_kept_per_entrypoint_and_never_sent`

## Row: `claude_code_local` via Desktop Code

**Client:** Desktop Code sessions opened on the enrolled directory; transcript
`entrypoint = "claude-desktop"`.

**State:** operative, pending CP-A/CP-B for real use. The capture reconciliation (capture
point 8) saw the same project hook fire under Desktop Code with the same payload fields, and
the transcripts land under the same projects root. No test drives a real Desktop Code
session; that proof is the post-CP-B step.

**Enrollment requirement:** the same as the CLI row. One enrollment and one hook serve both
clients, because both run the enrolled directory's project hook.

**Fails closed:** the same as the CLI row. The server never learns which client a line came
from (`entrypoint` is counted locally and never sent), so every server-side refusal applies
unchanged.

**Gap:** the same as the CLI row. Nothing keys on `SessionEnd.reason`, which is `other` under
Desktop Code and `prompt_input_exit` under the CLI.

**Not guaranteed:** the same four items as the CLI row: R4 (no hook-finality guarantee), R10
(token rotation is an operator step), one enrolled directory per bridge home, and the lossy
slug.

**Refusal:**
- `pytest:tests/postgres/test_local_authority.py::test_a_revoked_enrollment_refuses_enrollment_inactive`
- `pytest:tests/postgres/test_local_authority.py::test_the_refusal_vocabulary_is_exactly_the_six_words`
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_wrong_fingerprint_is_refused`
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_live_token_under_a_revoked_enrollment_is_refused`

**Replay:**
- `pytest:tests/postgres/test_evidence_ingest.py::test_a_replayed_batch_answers_the_same_and_writes_nothing_new`
- `pytest:tests/postgres/test_bridge_ingest_roundtrip.py::test_the_worker_drains_a_session_into_one_memory_per_turn`

**Fails-closed proof:**
- `pytest:tests/test_bridge_install.py::test_no_hook_installed_means_nothing_is_spooled_or_sent`

**Entrypoint:**
- `pytest:tests/test_bridge_transcript.py::test_entrypoint`
- `pytest:tests/test_bridge_worker.py::test_accepted_counts_are_kept_per_entrypoint_and_never_sent`

The per-entrypoint fixture reads a Desktop Code transcript beside a CLI one in the same
drain, counts `accepted:claude-desktop` apart from `accepted:cli`, and asserts that neither
`entrypoint` nor `claude-desktop` appears in any request body.
