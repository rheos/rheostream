# Shared application design

The app extends its existing token contract. Operate mode: task completion, clear state, and scanability guide the layout.

## Themes

GreenStream uses the established teal-ink ground and aqua accent, Geist, and modest rounded corners. It remains the default and is dark only.

Novadiem uses Geist too (#408; it used Sora, which read loose and wide at working sizes), with a restrained institutional character: navy and warm gold in dark mode, cream paper and ink in light mode. Both modes share type, spacing, tracking, and shape. Compact corners distinguish the family without adding decorative controls. The palette follows the publicly established Novadiem family; low intensity suits a working application.

All values live in validated theme files under `src/theme/themes`. The only runtime preference is a validated built-in identifier in a cookie set on the deployment's base host, so every module host shares it (#408). Confirmation tokens stay identical across themes. Fonts are vendored with their licenses; no external font requests are made.

## Composition

One shared navigation entry per enabled module. Module-local navigation provides individual views. Workspace, appearance and sign-out sit behind one settings button at the header's end; the workspace switcher appears there only for an account with more than one workspace. Leads defaults to an opportunity list with title filtering and pipeline selection; the board is an alternate, paginated view. Horizontal scrolling is confined to the board, the table, and the Leads view and tab bars on narrow displays.

Detail puts source evidence and notes in the wider column and stage actions and qualification in the second. On narrow screens these become one column. Native disclosure sections keep less common edits and draft history available without modal interruptions.

A job-search opportunity leads with what deciding on it needs: the title, its figures, a one-line verdict, the score and the stage controls, then the Posting, Questions, Proposal and Pricing tabs. Supporting detail (the score's evidence, the record facts, each tab's explanation) waits behind small info buttons, and the generic record sits in one closed disclosure at the foot of the page. Type stays in one family with sentence-case labels; no uppercase tracked micro-labels.

## Interaction and states

Forms retain errors inline and announce results. Pending submission disables that form; unavailable results warn against blind retries. A captured inquiry has a receipt-status page, with links to actual opportunities after processing. Only transitions allowed by the pinned preset are offered. Unknown qualification dimensions remain explicitly unknown.

No decorative page-load animation. Controls use a brief color transition unless reduced motion is preferred. Every action remains keyboard accessible.

## Sources and scope

The interface is newly written. The predecessor informed workflow density and evidence placement; no predecessor code or personal data was imported. Geist provenance is recorded in `src/theme/font.ts`.
