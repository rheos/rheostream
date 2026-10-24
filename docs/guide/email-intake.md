# Email intake

## What it is

The rules an agent follows to turn email into work items in [Current](current.md). An
agent with mail access reads a window of recent threads, decides which ones need someone to
act, and files each of those as one item through Current's `current_*` tools. rheoStream
has no built-in email adapter yet (see [intake and events](../architecture/intake-and-events.md));
these rules describe the agent routine that does the job today. A workspace keeps its own
sender map, noise list and addresses privately; this page holds only the general rules.

## What you can do with it

- Run intake on a schedule or on demand, over a lookback window you choose.
- Get one work item per email thread, filed at a priority that says how urgent it is.
- Re-run any window safely: filing the same thread again never makes a duplicate.
- See new mail on a thread you already filed as a short comment on its item, and see a
  finished item reopen when the other side writes again.

## How it works

### The lookback

Each run is given a lookback in whole hours and reads mail newer than that. Billing and
domain mail is always read over at least 72 hours, whatever the lookback, so a bill sent
before a short window still gets filed. The runner owns the schedule; a run never works out
its own window from a clock or from a previous run.

### The filing decision

Every thread goes one of three ways:

- **File at full priority** (critical, high or normal) when there is a concrete action:
  pay a bill, reply to a lead, fix an error, decide on a renewal.
- **File at low** when there is nothing to do but it is worth a record: a domain was
  registered, a security event already handled, a meaningful status update.
- **Skip** pure noise: marketing, promotions, routine receipts for small recurring charges,
  mail meant for someone else. Noise that is obvious from the sender and subject alone is
  skipped without opening the thread.

A thread whose newest message from someone else adds no action is skipped too: an
auto-reply, an out-of-office, an acknowledgement, a receipt, or a "paid", "resolved" or
"closed" notice. Filing it would reopen a finished item with nothing left to do. The
exception is an auto-reply that follows an open request made inside this run's window,
which no earlier run saw. A handled or status notice that is the only message in its
thread is still filed at low.

### Priority tiers

| Priority | Use it for |
| --- | --- |
| critical | Overdue, failed or down only: money past due or failed, a service that is down, a failed deploy of a live app |
| high | Due soon, an action needed now, a lead waiting for a reply |
| normal | Routine work with no rush: a statement to check, a renewal weeks away |
| low | A record with no action, or an event that has already happened |

An upcoming bill is never critical; a past-due one is. A failing check on a branch or an
error alert while the site is up is never critical. Between critical and high, choose high.

### Events already past

Before filing a notice with a date and time, check whether the event has happened. A past
event is filed at low with a past-tense title ("Building power shutdown was Oct 9, 2026").
This never applies to money: a bill past its due date is past due, and stays critical.

### One thread, one item

Each item carries the thread id as its source reference and the newest message id from
someone other than the workspace owner as its fingerprint. Current finds an existing item
by source and decides what a repeat means (see [repeated signals](current.md#repeated-signals-become-one-item)).
The owner's own replies never change the fingerprint, so answering a thread does not
reopen its item. If a thread holds two actions, the item's body names both.

### The fields

| Field | Value |
| --- | --- |
| source kind | `email` |
| source reference | the thread id, never a message id |
| fingerprint | the newest message id the owner did not send; the first message id when every message is the owner's |
| provenance | `email` |
| area | from the workspace's sender map |
| priority | from the tier table |
| title | one line: who, the amount and an absolute date; no account or card number |
| body | first line `Type: <label>`, then one fact per line, with the main link |
| due date | only when the email states one, never inferred |

The `Type:` line is plain text in the body. Current has no type field.

### The gist comment

A repeat never rewrites an item's body. So after a create that reopens, wakes or groups an
item on a new message, the agent adds one short comment with the gist of that message: who
wrote, what changed, any new date or amount. This comment is separate from the system
comment Current writes for the repeat, and both are kept.

The gist is its own operation, so it can fail after the create has succeeded. Repeating the
create does not bring it back: the fingerprint is already seen, so Current only touches the
update time. Instead, a later run adds a missing gist when the create returns grouped and
the item's newest comment is still Current's system comment for that same event. If someone
has commented on the item in between, the missed gist is not added.

### Failures and the result line

If the first call to Current is refused, or any mail search or read fails, the run files
nothing more and reports failed. A run reports success with nothing filed only when every
search and read worked. A run ends with one result line: created, grouped, reopened,
woke, skipped and failed counts, and created and reopened items by area. A failed run is simply
read again next time. The thread id and fingerprint make every create safe to repeat; a gist
that failed is recovered by the rule above, not by the repeat.

## Why it's built this way

Mail is where most work arrives, and a thread is easy to lose there. Filing everything turns the
work list into a second inbox; filing too little loses the bill that mattered. The action
gate keeps the list to things someone has to do, and Current's source dedup means a thread
that keeps coming back stays one item that climbs in priority or reopens, never a pile of
copies. The rules live with the agent, outside the database, so each workspace can tune its
sender map and noise list without a schema change.

## Where to look

- Current's side: [Current](current.md), [module guide](../../modules/current/README.md),
  [item operations](../../modules/current/src/rheo_current/operations.py)
- Audit of every create: [intake and events](../architecture/intake-and-events.md)
- Review: [the email-intake workflow map](../review/README.md)
