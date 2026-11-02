// Minimal Chrome DevTools Protocol client for the local acceptance audit browser.
//
// Node built-ins only (global fetch and WebSocket). It talks to a throwaway Chrome
// profile that `tests/harness/prompt14.sh browser-start` launched on a loopback
// DevTools port. It never prints, logs or stores a cookie value.
//
//   node audit_browser.mjs set-cookie PORT            < secret on stdin
//   node audit_browser.mjs cookie-names PORT
//   node audit_browser.mjs open PORT URL [PHRASE ...]
//   node audit_browser.mjs switch-workspace PORT WORKSPACE_ID

import { Buffer } from "node:buffer";
import process from "node:process";

const BASE_URL = "http://localhost:3000/";
const TIMEOUT_MS = 30_000;

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

async function discover(port, path) {
  const response = await fetch(`http://127.0.0.1:${port}${path}`);
  if (!response.ok) fail(`DevTools endpoint ${path} answered ${response.status}`);
  return response.json();
}

class Session {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = [];
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result);
      } else if (message.method) {
        for (const listener of this.listeners) listener(message);
      }
    });
  }

  opened() {
    return new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", () => reject(new Error("DevTools socket error")), {
        once: true,
      });
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  waitFor(method) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`timed out waiting for ${method}`)), TIMEOUT_MS);
      this.listeners.push((message) => {
        if (message.method === method) {
          clearTimeout(timer);
          resolve(message.params);
        }
      });
    });
  }

  close() {
    this.socket.close();
  }
}

async function browserSession(port) {
  const version = await discover(port, "/json/version");
  const session = new Session(version.webSocketDebuggerUrl);
  await session.opened();
  return session;
}

async function pageSession(port) {
  const targets = await discover(port, "/json/list");
  const page = targets.find((target) => target.type === "page");
  if (!page) fail("no page target in the audit browser");
  const session = new Session(page.webSocketDebuggerUrl);
  await session.opened();
  await session.send("Page.enable");
  await session.send("Runtime.enable");
  return session;
}

async function evaluate(session, expression) {
  const answer = await session.send("Runtime.evaluate", { expression, returnByValue: true });
  return answer.result.value;
}

const SNAPSHOT = `(() => ({
  url: location.href,
  title: document.title,
  headings: [...document.querySelectorAll("h1, h2, h3")].map((h) => h.textContent.trim()),
  workspace: document.querySelector("#rheo-workspace")?.value ?? null,
  text: document.body.innerText,
}))()`;

async function readStdin() {
  // A pipe can be non-blocking, so read it as a stream rather than readFileSync(0).
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

async function setCookie(port) {
  const secret = (await readStdin()).trim();
  if (!/^[0-9a-f]{32,}$/.test(secret)) fail("no session secret arrived on stdin");
  const session = await browserSession(port);
  // The same attributes core sets: HttpOnly, SameSite=Lax, Path=/, host-only, and
  // no Secure flag on the plain-http local host.
  await session.send("Storage.setCookies", {
    cookies: [
      {
        name: "rheo_session",
        value: secret,
        url: BASE_URL,
        path: "/",
        httpOnly: true,
        secure: false,
        sameSite: "Lax",
      },
    ],
  });
  session.close();
  process.stdout.write("rheo_session cookie installed for http://localhost\n");
}

async function cookieNames(port) {
  const session = await browserSession(port);
  const { cookies } = await session.send("Storage.getCookies");
  session.close();
  const names = cookies.map((c) => ({ name: c.name, domain: c.domain, httpOnly: c.httpOnly }));
  process.stdout.write(`${JSON.stringify(names)}\n`);
}

async function open(port, url, phrases) {
  const session = await pageSession(port);
  const loaded = session.waitFor("Page.loadEventFired");
  await session.send("Page.navigate", { url });
  await loaded;
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  const page = await evaluate(session, SNAPSHOT);
  session.close();
  const found = Object.fromEntries(phrases.map((phrase) => [phrase, page.text.includes(phrase)]));
  const { text: _text, ...rest } = page;
  process.stdout.write(`${JSON.stringify({ ...rest, found })}\n`);
}

async function switchWorkspace(port, workspaceId) {
  const session = await pageSession(port);
  const reloaded = session.waitFor("Page.loadEventFired");
  // The ordinary workspace switcher: choose the workspace and press Switch.
  const clicked = await evaluate(
    session,
    `(() => {
      const select = document.querySelector("#rheo-workspace");
      if (!select) return "no-switcher";
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
      setter.call(select, ${JSON.stringify(workspaceId)});
      select.dispatchEvent(new Event("change", { bubbles: true }));
      select.form.querySelector('button[type="submit"]').click();
      return "clicked";
    })()`,
  );
  if (clicked !== "clicked") fail(`workspace switch failed: ${clicked}`);
  await reloaded;
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  const page = await evaluate(session, SNAPSHOT);
  session.close();
  process.stdout.write(`${JSON.stringify({ url: page.url, workspace: page.workspace })}\n`);
}

const [command, port, ...rest] = process.argv.slice(2);
if (!/^[0-9]+$/.test(port ?? "")) fail("usage: audit_browser.mjs COMMAND PORT ...");
try {
  if (command === "set-cookie") await setCookie(port);
  else if (command === "cookie-names") await cookieNames(port);
  else if (command === "open" && rest.length >= 1) await open(port, rest[0], rest.slice(1));
  else if (command === "switch-workspace" && rest.length === 1) await switchWorkspace(port, rest[0]);
  else fail(`unknown command: ${command}`);
} catch (error) {
  fail(error instanceof Error ? error.message : String(error));
}
process.exit(0);
