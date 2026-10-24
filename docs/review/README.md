# Review package

A check of whether rheoStream's database holds the way its workflows are meant to run.
Each workflow is drawn twice: once as it should run, from rheoStream's own docs, and once
traced through the real tables. Every step is marked as having a home in the schema,
partly having one, or none yet. The worked examples use made-up people, companies and
mail; the rules and tables are real, read at commit `72b93ea`.

The full page, with tabs, the step-by-step tables, the decision tables and hover notes on
every flowchart shape, is [workflow-maps.html](workflow-maps.html). Open it in a browser
from a local checkout. The images below come from that page.

## What's here so far

| Part | State |
| --- | --- |
| Workflow maps: email intake into Current | Drawn |
| Workflow maps: the Upwork job feed through Leads, an agent action through approval, memory capture through recall | Not drawn yet |
| Schema map, architecture, delivery | Not drawn yet |

Tracking issue: #469.

## Email intake into Current

The rules come from [email intake](../guide/email-intake.md) and the repeated-signals
section of [Current](../guide/current.md#repeated-signals-become-one-item). Of 17 rules,
12 have a home in the schema and 5 partly have one. None is missing. Of the 21 decision
branches, 19 are fully handled.

### What it should do

One morning's run for a made-up two-person studio: a reminder for a bill filed yesterday,
a new lead, a client writing again on finished work, a "payment received" notice that
must not reopen anything, a promotion skipped unread, and an event that has already
happened.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/email-expected-dark.png">
  <img alt="The intake run as it should go: six threads, the filing decision for each, what each is filed as, what Current does, and what happens after the run" src="images/email-expected-light.png">
</picture>

### The same run in the schema

Every box from the picture above, outlined by how well Current's tables hold it, with the
table or column written in it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/email-schema-dark.png">
  <img alt="The same run traced through Current's tables: the mail and the filed items have a home; the filing decisions, the skipped threads and the result line are partly covered" src="images/email-schema-light.png">
</picture>

### Flowchart

One thread through one run, from the mail search to the result line.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/email-flow-dark.png">
  <img alt="Flowchart of one thread through an intake run: noise check, read, the action gate, the priority, the create, and what Current does with a repeat" src="images/email-flow-light.png">
</picture>

### What stands out

- **Repeats are handled in the database, not left to the agent.** A unique index makes
  one thread one item, done and snoozed items included. Fingerprints decide whether a
  repeat groups, reopens or wakes, the priority only climbs, and a repeat never rewrites
  what someone wrote.
- **The intake rules live outside the database.** The action gate, the priority tiers,
  the sender map and the mail types are agent rules and workspace configuration. The
  database keeps the result but not the reason, and a skipped thread leaves no trace.
- **A run leaves no record inside rheoStream.** Each create has its own audit row, but the
  run's window, its counts and a failed run exist only in the result line it prints.

### Questions

1. Should a "paid" or "resolved" notice close its item through
   `current_close_by_source`, instead of being skipped?
2. Should a run leave a record in rheoStream, so the screens can show when mail was last
   read and whether the last run failed?
3. Should the mail type be a field, so the queue can filter by it?
4. Should the sender map live in the workspace, where the screens can show and edit it?

## How the images are made

`workflow-maps.html` has an export mode that shows one diagram alone:
`?export=email-expected`, `email-schema` or `email-flow`, with `&theme=light` or
`&theme=dark`. Serve this folder locally and screenshot each one at 2x, for example:

```sh
python3 -m http.server 8941 --bind 127.0.0.1
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --hide-scrollbars --force-device-scale-factor=2 --window-size=1316,640 \
  --screenshot=images/email-expected-light.png \
  "http://127.0.0.1:8941/workflow-maps.html?export=email-expected&theme=light"
```

The flowchart needs `--window-size=1316,1980`.
