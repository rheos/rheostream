# Theme token contract, version 1

**Status:** Shipped. Decision [A18](README.md#architecture-decisions). Resolves issue #40, which
amended requirements decision D8 from "one theme" to a themable contract; the amendment is
recorded in the [requirements](../requirements/requirements-and-scope.md) D8 row, the
[build plan](../requirements/build-plan.md) phase-two scope and criteria 34 and 37, and the
[idea document's decision ledger](../ideas/rheo-stream-idea.md#recorded-changes-of-direction).

**Code:** `packages/web-contract/theme/` (`contract-v1.json`, `validate.ts`, `compile.ts`,
imported as `@rheo-stream/web-contract/theme`). The shipped code is the authority; this document
describes it.

A theme sets color, type and shape. It does not carry arbitrary CSS. Module screens are written
against the tokens the shell promises, so the token set is an interface with the same standing as
module contract version 1: it is versioned, and it stays small, because every token added is a
token every shared theme must then supply.

## A theme is data

```json
{
  "contract": 1,
  "id": "example-theme",
  "name": "Example theme",
  "description": "Optional free text.",
  "scheme": "dark",
  "tokens": { "color.ground": "#06181b", "…": "…" }
}
```

`scheme` is `"dark"` or `"light"`. `tokens` maps each contract token name to a value. The
contract itself, `contract-v1.json`, is a list of `{name, kind, reserved?}` entries. A token named
`a.b-c` compiles to the CSS custom property `--rs-a-b-c`.

## The tokens

Forty-three tokens in six groups.

| Group | Count | Tokens (kind) |
| --- | --- | --- |
| Palette | 10 | `color.ground`, `color.surface-1`, `color.surface-2`, `color.ink`, `color.ink-dim`, `color.ink-faint`, `color.accent`, `color.accent-strong`, `color.on-accent`, `color.hairline` (color) |
| State | 4 | `color.success`, `color.warning`, `color.danger`, `color.info` (color) |
| Form controls | 4 | `color.control-bg`, `color.control-border`, `color.control-focus`, `color.control-placeholder` (color) |
| Type | 8 | `font.sans`, `font.mono` (font stack); `font.size-sm`, `font.size-md`, `font.size-lg`, `font.size-xl`, `font.size-display`, `font.tracking-display` (length) |
| Shape and density | 11 | `radius.sm`, `radius.md`, `radius.lg`, `space.1` to `space.6`, `size.hairline`, `size.control` (length) |
| **Chrome, reserved for built-in themes** | 6 | `chrome.surface`, `chrome.ink`, `chrome.border`, `chrome.danger` (color); `chrome.font` (font stack); `chrome.font-size` (length) |

The chrome tokens exist for the approval and confirmation surface. A third-party theme cannot
change how a destructive or financial confirmation reads, which is what keeps the rule in
[confirmation and safety](confirmation-and-safety.md) enforceable: the interface restates the
bound values verbatim at the point of confirmation, in chrome no user theme controls.

## The API

```ts
validateTheme(data: unknown, options: { origin: "built-in" | "third-party" })
  -> { ok: true; theme: ThemeFile } | { ok: false; errors: ThemeError[] }

compileTheme(base: ThemeFile, overlay?: ThemeFile) -> string
```

`ThemeError` is `{ token: string | null; field?: string; reason }`, with `reason` one of
`contract-version`, `missing`, `unknown`, `invalid-value`, `reserved`. `token` names the contract
token at fault; for a theme-level problem it is `null` and `field` names the top-level key
instead. On success, `theme` is a fresh typed copy holding only the checked fields.

`origin` is the caller's knowledge, never a field the theme file can claim for itself: the
application decides it from its own registry of built-in themes.

`compileTheme` returns the text of a `<style>` element: the tokens of `base`, which must be a
built-in theme, with `overlay` layered over every non-chrome token when one is given.

## Validation rules

- `contract` must be `1`, or validation stops with `contract-version`.
- `id` and `name` must be non-empty strings, and `description`, when present, a string. Each
  failure is refused `invalid-value`, with `field` naming the key.
- Every non-reserved token must be present, or it is refused `missing`, naming the token.
- Unknown keys are refused `unknown`, both at the top level and inside `tokens`. A typo is caught,
  and nothing outside the contract can be carried in.
- A built-in theme must also carry every chrome token.
- A third-party theme that sets any `chrome.*` token is refused `reserved`, whatever the value.
- Every value must match its kind's grammar, or it is refused `invalid-value`. So must `scheme`.

## Value grammar

The compiled output lands in a `<style>` element. Values are never escaped, only
grammar-checked, so the grammars are the CSS-injection guard. `validateTheme` and `compileTheme`
check values through the same function, `isValidTokenValue`, and `scheme` through
`isThemeScheme`.

- **Color.** `#rrggbb`, `#rrggbbaa`, or `rgb(…)`/`rgba(…)` matched whole-value and
  case-sensitively by
  `^rgba?\([ \t\n\r\f]*(\d{1,3})[ \t\n\r\f]*,[ \t\n\r\f]*(\d{1,3})[ \t\n\r\f]*,[ \t\n\r\f]*(\d{1,3})[ \t\n\r\f]*(?:,[ \t\n\r\f]*(0|1|0?\.\d+|1\.0+)[ \t\n\r\f]*)?\)$`,
  with each channel then checked 0 to 255. Whitespace is spelled out as CSS whitespace
  (space, tab, line feed, carriage return, form feed) rather than JavaScript's `\s`, which also
  admits no-break space and U+2028. The alpha form `0?\.\d+` admits `.10`.
- **Length.** `-?\d+(\.\d+)?(px|rem|em)`. Negative only for `font.tracking-display`.
- **Font stack.** A comma-separated list in which each entry, after trimming spaces and tabs, is
  a bare family name (words of letters, digits, `_` and `-`), a quoted name, or exactly
  `var(--rs-font-brand)`. A quoted name must be closed by the same quote it opened with, and may
  not contain that quote, a line break, a form feed, or any of `; { } ( ) \ <`. A line feed,
  carriage return or form feed anywhere in the value refuses the whole stack: CSS treats a form
  feed as a newline too, and an unescaped newline inside a string ends it as a bad-string token.
  A quoted name containing a comma is refused, because it splits into two unterminated entries;
  that is the safe direction.
- **Scheme.** Exactly `dark` or `light`.

`var(--rs-font-brand)` is not a contract token. It is the custom property the application's own
font loader sets for the vendored brand font, and it is the only `var(…)` a theme may write.

## How the chrome is protected

`compileTheme` emits two `:root` rules: the six chrome declarations first, in a rule of their own,
then every other token followed by `color-scheme`.

At import, `validateTheme`'s grammars and its `reserved` refusal are the first line; behind them
`compileTheme` holds two defences of its own that do not depend on validation having run.

1. **Grammar re-check.** `compileTheme` re-checks every value it emits, from `base` and
   `overlay`, chrome tokens and non-chrome tokens alike, plus the resolved `scheme`, against the
   same grammars `validateTheme` uses (`isValidTokenValue`, `isThemeScheme`), and throws on any
   mismatch. This is the defence that matters. None of the grammars admits `;`, `{` or `}`, so no
   value, whether from a caller that skipped `validateTheme` entirely or from a theme built by
   hand, can end its own declaration and open a second one, such as a smuggled
   `--rs-chrome-ink` redeclaration inside the later, non-chrome rule.
2. **Source.** Chrome values are read from `base.tokens` only. There is no code path in
   `compileTheme` that reads a `chrome.*` key off `overlay` at all.

**The emission order is layout, not a defence.** As `compile.ts` itself says, a later rule's
redeclaration of a custom property wins in the ordinary cascade. Putting chrome first, on its own,
could not have stopped an `overlay` value such as `#000; --rs-chrome-ink: red` from overriding the
chrome rule's declaration for a caller that skipped validation: the injected declaration would sit
in the second rule and win. The grammar re-check exists for exactly that reason. Rule order was
never protective by itself.

### Residual: a theme can still choose the page's `color-scheme`

Validation and `compileTheme`'s two defences protect the chrome token **values**. They do not cover everything about how the
chrome looks.

A theme's `scheme` compiles to the CSS `color-scheme` property in the second, non-chrome rule,
sourced from `overlay?.scheme ?? base.scheme`. So a third-party theme can choose the page's overall
`color-scheme`. That property cascades page-wide, and it governs how the browser itself draws every
user-agent-rendered control on the page: native checkboxes, radio buttons, scrollbars, and
form-control chrome, whichever rule declared it and whatever element the control sits under. Any
part of a confirmation or approval screen that relies on native browser rendering, rather than an
explicit token-driven style, can therefore still shift visually when a third-party theme sets a
different `scheme`. This is a separate channel from the token values, and the defences above do
not close it.

No chrome-consuming component exists yet, so nothing is exposed today. The first change that
ships a confirmation or approval screen chooses one of two ways to close it:

- the reserved chrome set declares its **own** explicit `color-scheme` from a built-in-only
  value, isolating its user-agent-drawn parts from whatever `scheme` an overlay chose for the rest
  of the page; or
- the confirmation interface avoids native user-agent rendering entirely for chrome elements and
  tokenises every part of it, for example a checkbox styled from `chrome.*` tokens rather than
  left to the browser's `color-scheme`-dependent native rendering.

This choice is tracked in [Deferred](#deferred) below.

## The seed default

The application ships one built-in theme, `seed-dark`, in
`apps/web/src/theme/themes/seed-dark.json`, registered in `apps/web/src/theme/builtins.ts`. It is
**the seed default that a later UI phase replaces or extends**, not the final visual canon: that
phase adds its built-in themes as further entries in the same registry, and a third-party theme
needs no contract change at all.

Eight of its colors are taken verbatim from the project site's design tokens (`ground`,
`ground-raised` as `surface-1`, `ink`, `ink-dim`, `ink-faint`, `accent`, `accent-strong`,
`hairline`). Every other value is derived from those tokens' own rules and labelled as derived in
`builtins.ts`, so the later phase can see which values are sourced and which are extrapolated. A
contrast test holds every text color, the state colors included, at WCAG AA (4.5:1) or better on
all three surfaces, and the control border and focus colors at the 3:1 non-text floor.

The root layout validates the seed as a built-in theme and compiles it once at module load. A
built-in theme that fails validation throws there, which fails the build or the server's boot
rather than rendering an unstyled page.

## Writing against the contract

- Every color, font and spacing value in a shell or module screen comes from a `var(--rs-…)`
  token. No literal. `scripts/check_theme_tokens.py` enforces this over `apps/web/src` and
  `modules/*/web`, and until a chrome consumer ships it refuses every `--rs-chrome-*`
  reference there.
- Separate nested surfaces with the `color.hairline` edge, not background alone. In the seed,
  `surface-2` is only about 1.03:1 against `surface-1`: it is held dark enough that the verbatim
  `ink-faint` keeps AA on it, so a background change by itself does not read as an edge.
- The contract has no border or ring width beyond `size.hairline` (1px). The global focus ring
  uses the CSS keyword `medium` for its width until a later contract version adds a token.

## Changing the contract

Adding, removing or renaming a token, or changing a kind's grammar, is a new contract version.
Version 1 validates and compiles version-1 themes only.

## Deferred

- Theme selection (a settings key under the operator policy floor), importing a user theme with
  text-on-surface contrast validation, and the built-in themes on this same contract: a later UI
  phase, tracked as #113. Two built-ins are planned to start, both working
  names: the current rheo.stream site look, dark only, which `seed-dark` seeds, and a Novadiem
  theme with a light and a dark mode. Each mode is its own theme file on contract v1; grouping the
  pair as one theme behind a user-facing switch is later registry work, not a contract change.
- A chrome-token consumer and a lint rule scoping `chrome.*` tokens to a `chrome/` directory: the
  first change that ships a confirmation or approval screen. That change also closes the
  [`color-scheme` residual](#residual-a-theme-can-still-choose-the-pages-color-scheme) by one of
  the two routes named there.
