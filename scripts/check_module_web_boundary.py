#!/usr/bin/env python3
"""Module web boundary scan (#183): a module's web package may reach core only
through the `ShellApi` its screens are handed.

The #122 allowlist gates `ShellApi.call`. Module web code runs inside the web tier's
own process, though (apps/web transpiles workspace packages), so a package that skips
`ShellApi` could import the app's operation client through the `@/` alias, or read
`RHEO_INTERNAL_SECRET` and the session cookie and call the internal listener as the
signed-in user. This gate refuses every such path statically.

**What is scanned.** For each `modules/*/web` package, every file the web app can
load: the package's entry points (`exports`, `main`, `module`, `browser` in its
`package.json`) and everything they import, transitively. Imports of
`@rheo-stream/web-contract` subpaths are followed into that package and scanned under
the same rules, so the contract cannot become a laundering channel either. Tests and
test helpers the entry points never import are out of scope, because the web app never
loads them; a production file that imports one brings it into scope.

**Imports.** Every static `import … from`, `export … from`, side-effect `import "…"`
and literal dynamic `import("…")` specifier must be one of:

- a relative path that resolves to a file inside the same package (a path that
  leaves the package, or does not resolve, is refused);
- `react`;
- a subpath that `packages/web-contract/package.json` exports, such as
  `@rheo-stream/web-contract/screen`.

Anything else is refused, and the common escapes get a named reason: the `@/` alias
into apps/web, `next/headers`, `next/server` and every other `next` path, Node
built-ins (bare or `node:`), and any other package. Type-only imports are held to
the same rule. A dynamic `import()` whose argument is not a string literal, and every
`require(`, are refused because their target cannot be read statically. Widening the
allowlist is a one-line change to `_ALLOWED_BARE` here, which is the review point.

**Code.** After comments are stripped and string contents blanked (template-literal
`${…}` expressions stay visible), these are refused:

- a free reference to `fetch`, `XMLHttpRequest`, `WebSocket`, `WebSocketStream`,
  `EventSource`, `WebTransport`, `RTCPeerConnection`, `Worker`, `SharedWorker`,
  `importScripts`, `navigator` or `cookieStore`;
- a free reference to `process` (so `process.env`, `const { env } = process`);
- `globalThis`, `eval` and `Function`, which reach any of the above by computed name;
- `window`, `self` or `global`, unless the reference is a plain member access to a
  name not listed above, or declares a local (`const window = …`) or a typed
  parameter (`(shell, window: ReadWindow)`). Recallatron's browse loader names a
  local `window`, so `window.items` passes, while `window.fetch`, `self[…]`,
  `const w = window`, `const { location } = window` and `f(self)` are refused;
- any `.cookie` member access, and `document[…]`;
- `import.meta`;
- a `"use server"` directive, which would publish a server function from the web
  process.

A "free" reference is one not preceded by `.`, so `obj.fetch`, `refetch` and
`prefetchAll` are not refused. A property key spelled like a listed name
(`{ process: 1 }`, `type T = { fetch(): void }`) is refused too; rename the field.

Known limit: the scan reads source text, not types or values. It does not see a name
assembled at runtime and passed through an allowed object, and a regex literal
containing a quote can confuse the string blanker. Every form #183 names is caught.
Core still checks roles and token sets on each call.

At least one `modules/*/web` package must exist, or the scan reports that rather than
passing over nothing. A package without a `package.json` or without an entry point
fails.

Runs under the system python3 (3.9-compatible, stdlib only), like the other
`scripts/check_*.py` gates, so it runs before any dependency is installed. The
quote-aware stripper is a local extension of the `_strip_ts_comments` copies in the
sibling gates (see scripts/README.md): it also returns a string-blanked view.

Self-test (anti-vacuity): `main()` always builds scratch repository trees, plants
every refused form in a file reached from a scratch module's entry point, plus
near-misses that must pass, an unreachable test file that must be ignored, a
laundering helper reached only transitively, a violation inside the followed
web-contract package, and the missing-package and missing-entry-point cases. Only
then does it scan the real tree.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CONTRACT_NAME = "@rheo-stream/web-contract"
_CONTRACT_DIR = Path("packages") / "web-contract"
_ALLOWED_BARE = frozenset({"react"})

_CODE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_DATA_SUFFIXES = (".json", ".css")
_RESOLVE_SUFFIXES = (".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs", ".cjs", ".json")

_NODE_BUILTINS = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "timers",
        "tls",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

# Free identifiers that reach the network, the environment, or code by name.
_FORBIDDEN_FREE = {
    "fetch": "calls a network API directly (fetch)",
    "XMLHttpRequest": "calls a network API directly (XMLHttpRequest)",
    "WebSocket": "calls a network API directly (WebSocket)",
    "WebSocketStream": "calls a network API directly (WebSocketStream)",
    "EventSource": "calls a network API directly (EventSource)",
    "WebTransport": "calls a network API directly (WebTransport)",
    "RTCPeerConnection": "calls a network API directly (RTCPeerConnection)",
    "Worker": "starts a worker, which can reach the network",
    "SharedWorker": "starts a worker, which can reach the network",
    "importScripts": "loads remote code (importScripts)",
    "navigator": "reads navigator (sendBeacon, serviceWorker)",
    "cookieStore": "reads cookies (cookieStore)",
    "process": "reads process (process.env)",
    "globalThis": "reaches globals by name (globalThis)",
    "eval": "evaluates code by string (eval)",
    "Function": "evaluates code by string (Function)",
}
_GLOBAL_OBJECTS = ("window", "self", "global")

_FROM_SPEC = re.compile(r"\bfrom\s*([\"'])")
_SIDE_EFFECT_SPEC = re.compile(r"(?<![\w$.])import\s*([\"'])")
_DYNAMIC_IMPORT = re.compile(r"(?<![\w$.])import\s*\(")
_REQUIRE = re.compile(r"(?<![\w$.])require\s*\(")
_IMPORT_META = re.compile(r"(?<![\w$.])import\s*\.\s*meta\b")
_COOKIE_MEMBER = re.compile(r"\.\s*cookie(?![\w$])")
_DOCUMENT_COMPUTED = re.compile(r"(?<![\w$.])document\s*(?:\?\.\s*)?\[")
_MEMBER_AFTER = re.compile(r"\s*(?:\?\.|\.)\s*([\w$]+)")
_DECLARES = re.compile(r"(?<![\w$])(?:const|let|var)\s+$")
_PARAMETER_BEFORE = re.compile(r"[(,]\s*$")
_ANNOTATED_AFTER = re.compile(r"\s*\??\s*:")
_USE_SERVER = re.compile(r"(?:^|[{;])\s*([\"'])use server\1", re.MULTILINE)


def _split_views(text: str) -> tuple[str, str]:
    """Return (comments stripped, comments stripped and string contents blanked).

    Both views keep the input's length and newlines, so an offset or line number in
    one is valid in the other and in the source. Template literals are tracked with a
    stack so a `${…}` expression stays visible as code in the blanked view.
    """
    keep: list[str] = []
    code: list[str] = []
    # Each entry: "t" for inside a template literal, or an int brace depth for code
    # inside a template's `${…}`. The base level is the empty stack.
    stack: list[object] = []
    i, n = 0, len(text)

    def blank(chunk: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in chunk)

    while i < n:
        top = stack[-1] if stack else None
        if top == "t":
            char = text[i]
            if char == "\\" and i + 1 < n:
                keep.append(text[i : i + 2])
                code.append(blank(text[i : i + 2]))
                i += 2
                continue
            if char == "`":
                keep.append(char)
                code.append(char)
                stack.pop()
                i += 1
                continue
            if text[i : i + 2] == "${":
                keep.append("${")
                code.append("${")
                stack.append(0)
                i += 2
                continue
            keep.append(char)
            code.append(blank(char))
            i += 1
            continue

        pair = text[i : i + 2]
        if pair == "//":
            end = text.find("\n", i)
            end = n if end == -1 else end
            keep.append(" " * (end - i))
            code.append(" " * (end - i))
            i = end
            continue
        if pair == "/*":
            close = text.find("*/", i + 2)
            end = n if close == -1 else close + 2
            keep.append(blank(text[i:end]))
            code.append(blank(text[i:end]))
            i = end
            continue
        char = text[i]
        if char in "\"'":
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == char:
                    j += 1
                    break
                if text[j] == "\n":
                    break
                j += 1
            chunk = text[i:j]
            keep.append(chunk)
            closed = len(chunk) >= 2 and chunk.endswith(char)
            inner = chunk[1:-1] if closed else chunk[1:]
            code.append(char + blank(inner) + (char if closed else ""))
            i = j
            continue
        if char == "`":
            keep.append(char)
            code.append(char)
            stack.append("t")
            i += 1
            continue
        if isinstance(top, int):
            if char == "{":
                stack[-1] = top + 1
            elif char == "}":
                if top == 0:
                    stack.pop()
                else:
                    stack[-1] = top - 1
        keep.append(char)
        code.append(char)
        i += 1
    return "".join(keep), "".join(code)


def _line(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _quoted_at(keep: str, quote_index: int) -> tuple[str, int] | None:
    """The string literal that opens at `quote_index`: (contents, end index)."""
    quote = keep[quote_index]
    end = keep.find(quote, quote_index + 1)
    newline = keep.find("\n", quote_index + 1)
    if end == -1 or (newline != -1 and newline < end):
        return None
    return keep[quote_index + 1 : end], end


def _free(code: str, start: int) -> bool:
    """True when the identifier at `start` is not a member access (`x.name`)."""
    j = start - 1
    while j >= 0 and code[j] in " \t\r\n":
        j -= 1
    return j < 0 or code[j] != "."


def _code_findings(relative: str, keep: str, code: str) -> list[str]:
    findings: list[str] = []

    def add(position: int, reason: str) -> None:
        findings.append(f"{relative}:{_line(code, position)}: {reason}")

    for name, reason in _FORBIDDEN_FREE.items():
        for match in re.finditer(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", code):
            if _free(code, match.start()):
                add(match.start(), reason)

    forbidden_members = {*_FORBIDDEN_FREE, "document", *_GLOBAL_OBJECTS}
    for obj in _GLOBAL_OBJECTS:
        for match in re.finditer(r"(?<![\w$])" + obj + r"(?![\w$])", code):
            if not _free(code, match.start()):
                continue
            after = code[match.end() :]
            member = _MEMBER_AFTER.match(after)
            if member is not None:
                if member.group(1) not in forbidden_members:
                    continue  # `window.items` on a local named `window`
                add(match.start(), f"reaches a global through {obj}.{member.group(1)}")
                continue
            before = code[max(0, match.start() - 40) : match.start()]
            if _DECLARES.search(before):
                continue  # `const window = …` declares a local
            if _PARAMETER_BEFORE.search(before) and _ANNOTATED_AFTER.match(after):
                continue  # `(shell, window: ReadWindow)` declares a parameter
            add(
                match.start(),
                f"uses the global {obj} other than through a plain member access "
                "(aliasing, computed access, or passing it on)",
            )

    for match in _COOKIE_MEMBER.finditer(code):
        add(match.start(), "reads a cookie (.cookie)")
    for match in _DOCUMENT_COMPUTED.finditer(code):
        add(match.start(), "reaches document by computed name")
    for match in _IMPORT_META.finditer(code):
        add(match.start(), "reads import.meta")
    for match in _REQUIRE.finditer(code):
        add(match.start(), "calls require(); module web packages use static imports")
    for match in _USE_SERVER.finditer(keep):
        add(match.start(1), 'declares a "use server" function in the web process')
    return findings


def _specifiers(keep: str, code: str) -> tuple[list[tuple[str, int]], list[int]]:
    """Every import specifier (text, offset), plus offsets of dynamic imports whose
    argument is not a plain string literal. Matched on the blanked view, so an
    `import` or `from` inside a string or comment is never mistaken for one; the
    specifier text is then read from the same offset in the kept view."""
    found: list[tuple[str, int]] = []
    non_literal: list[int] = []
    for pattern in (_FROM_SPEC, _SIDE_EFFECT_SPEC):
        for match in pattern.finditer(code):
            quoted = _quoted_at(keep, match.start(1))
            if quoted is not None:
                found.append((quoted[0], match.start(1)))
    for match in _DYNAMIC_IMPORT.finditer(code):
        j = match.end()
        while j < len(code) and code[j] in " \t\r\n":
            j += 1
        quoted = _quoted_at(keep, j) if j < len(code) and code[j] in "\"'" else None
        if quoted is None:
            non_literal.append(match.start())
            continue
        k = quoted[1] + 1
        while k < len(code) and code[k] in " \t\r\n":
            k += 1
        if k < len(code) and code[k] == ")":
            found.append((quoted[0], j))
        else:
            non_literal.append(match.start())
    return found, non_literal


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _resolve_file(base: str) -> str | None:
    """Resolve a normalised path the way a bundler does for a relative import."""
    candidates = [base]
    stem, suffix = os.path.splitext(base)
    if suffix in (".js", ".jsx", ".mjs", ".cjs"):
        # TypeScript's ESM convention: `./x.js` names `./x.ts`.
        candidates += [stem + ext for ext in (".ts", ".tsx", ".mts", ".cts")]
    candidates += [base + ext for ext in _RESOLVE_SUFFIXES]
    candidates += [os.path.join(base, "index" + ext) for ext in _RESOLVE_SUFFIXES]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _entry_points(package_json: dict) -> list[str]:
    entries: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            entries.append(value)
        elif isinstance(value, dict):
            for inner in value.values():
                collect(inner)
        elif isinstance(value, list):
            for inner in value:
                collect(inner)

    collect(package_json.get("exports"))
    for key in ("main", "module", "browser"):
        if isinstance(package_json.get(key), str):
            entries.append(package_json[key])
    return entries


def _contract_exports(repo_root: Path) -> dict[str, str] | None:
    """Map each exported contract subpath (`@rheo-stream/web-contract/screen`) to its
    file, or None when the contract package is missing or unreadable."""
    manifest = repo_root / _CONTRACT_DIR / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    exports = data.get("exports")
    if not isinstance(exports, dict):
        return {}
    mapping: dict[str, str] = {}
    for key, value in exports.items():
        targets: list[str] = []
        if isinstance(value, str):
            targets = [value]
        elif isinstance(value, dict):
            targets = [v for v in value.values() if isinstance(v, str)]
        if not key.startswith("./") or "*" in key or not targets:
            continue
        mapping[_CONTRACT_NAME + "/" + key[2:]] = os.path.normpath(
            str(repo_root / _CONTRACT_DIR / targets[0])
        )
    return mapping


def _bare_reason(spec: str) -> str:
    if spec.startswith("@/"):
        return "imports the web app's internals through the @/ alias"
    if spec in ("next/headers", "next/server"):
        return f"imports {spec}, which reads the request, session and cookies"
    if spec == "next" or spec.startswith("next/"):
        return f"imports {spec}; module screens use only the ShellApi they are handed"
    if spec.startswith("node:") or spec.split("/", 1)[0] in _NODE_BUILTINS:
        return f"imports the Node built-in {spec}"
    if spec.startswith("/"):
        return f"imports an absolute path {spec}"
    if spec == _CONTRACT_NAME or spec.startswith(_CONTRACT_NAME + "/"):
        return f"imports {spec}, which the web-contract package does not export"
    return f"imports {spec}, which is not on the module-web import allowlist"


def _relative_to(path: str, repo_root: Path) -> str:
    try:
        return Path(path).relative_to(repo_root).as_posix()
    except ValueError:
        return path


def _scan_package(
    package_dir: Path, repo_root: Path, contract: dict[str, str] | None
) -> list[str]:
    findings: list[str] = []
    manifest = package_dir / "package.json"
    package_rel = _relative_to(str(package_dir), repo_root)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        return [f"{package_rel}: no readable package.json ({error.__class__.__name__})"]
    entries = _entry_points(data if isinstance(data, dict) else {})
    if not entries:
        return [
            f"{package_rel}/package.json: declares no entry point (exports or main)"
        ]

    package_root = os.path.normpath(str(package_dir))
    contract_root = os.path.normpath(str(repo_root / _CONTRACT_DIR))
    queue: list[tuple[str, str]] = []  # (file, the package root it must stay inside)
    for entry in entries:
        target = os.path.normpath(os.path.join(package_root, entry))
        resolved = _resolve_file(target) if _within(target, package_root) else None
        if resolved is None:
            findings.append(
                f"{package_rel}/package.json: entry point {entry} does not resolve "
                "to a file inside the package"
            )
            continue
        queue.append((resolved, package_root))

    seen: set[str] = set()
    while queue:
        path, owner_root = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        if not path.endswith(_CODE_SUFFIXES):
            continue
        relative = _relative_to(path, repo_root)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            findings.append(f"{relative}: unreadable ({error.__class__.__name__})")
            continue
        keep, code = _split_views(text)
        findings.extend(_code_findings(relative, keep, code))
        specs, non_literal = _specifiers(keep, code)
        for position in non_literal:
            findings.append(
                f"{relative}:{_line(code, position)}: dynamic import() of a "
                "non-literal specifier"
            )
        for spec, position in specs:
            where = f"{relative}:{_line(code, position)}"
            if spec.startswith("./") or spec.startswith("../") or spec in (".", ".."):
                target = os.path.normpath(os.path.join(os.path.dirname(path), spec))
                if not _within(target, owner_root):
                    findings.append(
                        f"{where}: relative import {spec} leaves the package"
                    )
                    continue
                resolved = _resolve_file(target)
                if resolved is None:
                    findings.append(f"{where}: relative import {spec} does not resolve")
                    continue
                if not resolved.endswith(_CODE_SUFFIXES + _DATA_SUFFIXES):
                    findings.append(
                        f"{where}: imports {spec}, which is not source or data"
                    )
                    continue
                queue.append((resolved, owner_root))
                continue
            if spec in _ALLOWED_BARE:
                continue
            if contract is not None and spec in contract:
                queue.append((contract[spec], contract_root))
                continue
            if contract is None and spec.startswith(_CONTRACT_NAME + "/"):
                findings.append(
                    f"{where}: imports {spec}, but {_CONTRACT_DIR.as_posix()} "
                    "has no readable package.json to follow it into"
                )
                continue
            findings.append(f"{where}: {_bare_reason(spec)} ({spec})")
    return findings


def check(repo_root: Path) -> tuple[list[str], int]:
    """Scan every `modules/*/web` package under `repo_root`. Returns (findings,
    number of packages scanned)."""
    modules_root = repo_root / "modules"
    packages = sorted(p for p in modules_root.glob("*/web") if p.is_dir())
    if not packages:
        return [f"no module web package found under {modules_root}/*/web"], 0
    contract = _contract_exports(repo_root)
    findings: list[str] = []
    for package in packages:
        findings.extend(_scan_package(package, repo_root, contract))
    return sorted(set(findings)), len(packages)


# --- self-test ------------------------------------------------------------------------

_PKG = "modules/scratch/web"

# Each planted file sits in the scratch package's src/planted/ and is imported by the
# entry point, so it is in scope. Every one must be flagged.
_PLANTED = {
    "alias-import.ts": 'import { callOperation } from "@/lib/operations";\n',
    "alias-type-import.ts": 'import type { Session } from "@/lib/session";\n',
    "alias-multiline.ts": 'import {\n  callOperation,\n} from "@/lib/operations";\n',
    "alias-reexport.ts": 'export * from "@/lib/operations";\n',
    "alias-side-effect.ts": 'import "@/lib/session";\n',
    "alias-dynamic.ts": 'export const load = () => import("@/lib/core-client");\n',
    "dynamic-computed.ts": "export const load = (p: string) => import(p);\n",
    "next-headers.ts": 'import { cookies, headers } from "next/headers";\n',
    "next-server.ts": "import { NextResponse } from 'next/server';\n",
    "next-other.ts": 'import { redirect } from "next/navigation";\n',
    "escape-relative.ts": (
        'import { callOperation } from "../../../../../apps/web/src/lib/operations";\n'
    ),
    "unresolved.ts": 'import { x } from "./does-not-exist";\n',
    "node-prefixed.ts": 'import { request } from "node:https";\n',
    "node-bare.ts": 'import http from "http";\n',
    "third-party.ts": 'import axios from "axios";\n',
    "contract-internal.ts": (
        'import { x } from "@rheo-stream/web-contract/src/screen";\n'
    ),
    "require.ts": 'const ops = require("./does-not-exist");\n',
    "fetch-call.ts": 'export const f = () => fetch("/internal");\n',
    "fetch-reference.ts": "const f = fetch;\nexport default f;\n",
    "window-fetch.ts": "export const f = (u: string) => window.fetch(u);\n",
    "self-computed.ts": "export const f = () => self['fe' + 'tch'];\n",
    "window-alias.ts": (
        "const w = window;\nexport const f = (u: string) => w.location.href + u;\n"
    ),
    "window-destructure.ts": "export const { location: l } = window;\n",
    "window-argument.ts": "export const f = (g: (w: unknown) => void) => g(self);\n",
    "window-optional.ts": "export const f = (u: string) => window?.fetch(u);\n",
    "globalthis.ts": "export const f = () => globalThis;\n",
    "xhr.ts": "export const x = () => new XMLHttpRequest();\n",
    "websocket.ts": "export const w = (u: string) => new WebSocket(u);\n",
    "eventsource.ts": "export const e = (u: string) => new EventSource(u);\n",
    "beacon.ts": "export const b = (u: string) => navigator.sendBeacon(u, '');\n",
    "process-env.ts": "export const secret = process.env.RHEO_INTERNAL_SECRET;\n",
    "process-destructure.ts": "const { env } = process;\nexport default env;\n",
    "document-cookie.ts": "export const c = () => document.cookie;\n",
    "document-computed.ts": "export const c = () => document['coo' + 'kie'];\n",
    "template-expression.ts": "export const s = (u: string) => `${fetch(u)}`;\n",
    "eval.ts": "export const e = () => eval('1');\n",
    "function-constructor.ts": "export const f = () => new Function('return 1');\n",
    "import-meta.ts": "export const e = import.meta.env;\n",
    "use-server.ts": '"use server";\nexport async function act() {}\n',
    "use-server-inline.ts": "export async function act() { 'use server'; }\n",
}

# Reached from the entry point and must NOT be flagged.
_NEAR_MISSES = {
    "good-screen.tsx": (
        'import type { ReactNode } from "react";\n'
        'import type { ScreenProps } from "@rheo-stream/web-contract/screen";\n'
        'import styles from "./good.module.css";\n'
        'import data from "./data.json";\n'
        "export async function Good({ shell }: ScreenProps): Promise<ReactNode> {\n"
        '  const window = await shell.call("scratch.read", {});\n'
        "  return `${styles.x}${data.y}${window.items}${window.state}`;\n"
        "}\n"
    ),
    "prose.ts": (
        "// fetch() and process.env are off limits; so is import('@/lib/operations')\n"
        "/* window.fetch, document.cookie, next/headers */\n"
        'export const copy = "Never fetch(url), read process.env or document.cookie";\n'
        "export const quoted = 'import x from \"@/lib/session\"';\n"
        'export const tpl = `a fetch(url) in a template, from "next/headers"`;\n'
    ),
    "members.ts": (
        "type O = { refetch(): void; prefetch: number; fetchAll(): void };\n"
        "export const n = (o: O) => {\n"
        "  o.refetch();\n"
        "  o.fetchAll();\n"
        "  return o.prefetch;\n"
        "};\n"
        "export const processed = 1;\n"
        "export const reprocess = processed;\n"
        "export const selfish = { windowed: 1 };\n"
        "export const m = (o: Record<string, () => number>) =>\n"
        "  o.fetch() + o . process() + (o?.navigator() ?? 0) + o.cookieStore();\n"
    ),
    # Recallatron's browse loader names a local and a parameter `window`.
    "local-window.ts": (
        "function items(shell: unknown, window: { items: number[] }) {\n"
        "  return window.items.length;\n"
        "}\n"
        "export const load = () => {\n"
        '  const window = { state: "ok", value: { total: 1 } };\n'
        "  return window.state + window.value.total + items(null, { items: [] });\n"
        "};\n"
    ),
    "relative-ok.ts": 'export { helper } from "./helpers/index";\n',
    # Clean itself; the fetch it launders lives in ../testing/net.ts, checked below.
    "launder.ts": 'export { net } from "../testing/net";\n',
}

_EXTRA_FILES = {
    f"{_PKG}/src/planted/good.module.css": ".x { color: inherit; }\n",
    f"{_PKG}/src/planted/data.json": '{ "y": 1 }\n',
    f"{_PKG}/src/planted/helpers/index.ts": "export const helper = 1;\n",
    f"{_PKG}/src/testing/net.ts": "export const net = (u: string) => fetch(u);\n",
    # A clean file outside the package, so escape-relative.ts resolves: only the
    # leaves-the-package rule can catch it.
    "apps/web/src/lib/operations.ts": "export const callOperation = 1;\n",
    # Never imported by the entry point: the web app cannot load it.
    f"{_PKG}/src/untouched.test.ts": (
        'import { readFileSync } from "node:fs";\n'
        'import { callOperation } from "@/lib/operations";\n'
        "fetch(String(readFileSync) + String(callOperation) + process.env.X);\n"
    ),
    "packages/web-contract/package.json": json.dumps(
        {"name": _CONTRACT_NAME, "exports": {"./screen": "./src/screen.ts"}}
    ),
    "packages/web-contract/src/screen.ts": (
        'import type { ReactNode } from "react";\n'
        "export type ScreenProps = { shell: { call(o: string, a: object): Promise<"
        "{ items: ReactNode; state: string }> } };\n"
    ),
}


def _write(repo: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _self_test() -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        names = [*_PLANTED, *_NEAR_MISSES]
        entry = "".join(
            f'export * as m{i} from "./planted/{name.rsplit(".", 1)[0]}";\n'
            for i, name in enumerate(names)
        )
        files = {
            f"{_PKG}/package.json": json.dumps({"exports": {".": "./src/index.ts"}}),
            f"{_PKG}/src/index.ts": entry,
            **{f"{_PKG}/src/planted/{n}": t for n, t in _PLANTED.items()},
            **{f"{_PKG}/src/planted/{n}": t for n, t in _NEAR_MISSES.items()},
            **_EXTRA_FILES,
        }
        _write(repo, files)
        findings, count = check(repo)
        if count != 1:
            return f"self-test FAILED: expected 1 scratch package, scanned {count}"
        flagged = {f.split(":", 1)[0] for f in findings}

        missing = sorted(
            n for n in _PLANTED if f"{_PKG}/src/planted/{n}" not in flagged
        )
        if missing:
            return f"self-test FAILED: planted violations went uncaught: {missing}"
        wrong = sorted(n for n in _NEAR_MISSES if f"{_PKG}/src/planted/{n}" in flagged)
        if wrong:
            detail = [f for f in findings if any(n in f for n in wrong)]
            return f"self-test FAILED: near-miss files were flagged: {detail}"
        if f"{_PKG}/src/testing/net.ts" not in flagged:
            return (
                "self-test FAILED: a fetch in a helper reached only through a "
                "re-export went uncaught (imports are not followed)"
            )
        if f"{_PKG}/src/untouched.test.ts" in flagged:
            return "self-test FAILED: a file the entry point never imports was scanned"
        if f"{_PKG}/src/index.ts" in flagged:
            return f"self-test FAILED: the clean entry point was flagged: {findings}"
        if not any(
            f.startswith(f"{_PKG}/src/planted/escape-relative.ts:1:")
            and "leaves the package" in f
            for f in findings
        ):
            return (
                "self-test FAILED: a relative import into apps/web was not refused "
                "as leaving the package"
            )
        if any(f.startswith("apps/web/") for f in findings):
            return "self-test FAILED: an escaped import was followed out of the package"
        if not any(
            f.startswith(f"{_PKG}/src/planted/alias-multiline.ts:3:") for f in findings
        ):
            return "self-test FAILED: a multi-line import's line was not reported as 3"

        # A violation inside the followed web-contract package is caught too.
        _write(
            repo,
            {
                "packages/web-contract/src/screen.ts": (
                    "export const f = () => fetch('/x');\n"
                )
            },
        )
        findings, _ = check(repo)
        if not any(
            f.startswith("packages/web-contract/src/screen.ts:1:") for f in findings
        ):
            return (
                "self-test FAILED: a fetch inside the web-contract package went "
                "uncaught (contract imports are not followed)"
            )

    # A package with no entry point, and one with no package.json, both fail.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _write(
            repo,
            {
                "modules/a/web/package.json": json.dumps({"name": "a"}),
                "modules/b/web/src/index.ts": "export const x = 1;\n",
            },
        )
        findings, _ = check(repo)
        if not any(f.startswith("modules/a/web/package.json:") for f in findings):
            return "self-test FAILED: a package with no entry point was not reported"
        if not any(f.startswith("modules/b/web:") for f in findings):
            return "self-test FAILED: a package with no package.json was not reported"

    # No module web package at all is reported, not passed over.
    with tempfile.TemporaryDirectory() as tmp:
        findings, count = check(Path(tmp))
        if count != 0 or not findings:
            return "self-test FAILED: a tree with no module web package passed"
    return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        findings, count = check(ROOT)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Module web boundary check failed: {error}", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(
            "Module web boundary checks passed (self-test verified every planted "
            f"escape is caught): {count} module web package(s) reach core only "
            "through ShellApi."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
