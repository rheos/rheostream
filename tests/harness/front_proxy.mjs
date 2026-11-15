// The acceptance stack's single-origin front (test-only).
//
// A real deployment puts an operator-provided reverse proxy in front of both tiers:
// `/auth/*` on an application host goes to core, everything else to web
// (deploy/README.md, the routing table). The local stack has no such proxy, so a
// browser on http://localhost:3000 could not reach the ordinary `/auth/*` endpoints
// the web tier links to (the workspace switcher, logout, the continue hop). This
// front applies exactly that one rule, on the project network, with Node built-ins.

import http from "node:http";
import process from "node:process";

const CORE = { host: "core", port: 8000 };
const WEB = { host: "web", port: 3000 };
const LISTEN = 3000;

function upstreamFor(url) {
  return url === "/auth" || url.startsWith("/auth/") || url.startsWith("/auth?") ? CORE : WEB;
}

const server = http.createServer((request, response) => {
  const target = upstreamFor(request.url ?? "/");
  const upstream = http.request(
    {
      host: target.host,
      port: target.port,
      method: request.method,
      path: request.url,
      // The browser's own Host header goes through unchanged: both tiers derive
      // the session host and same-origin checks from it.
      headers: request.headers,
    },
    (answer) => {
      response.writeHead(answer.statusCode ?? 502, answer.headers);
      answer.pipe(response);
    },
  );
  upstream.on("error", () => {
    if (!response.headersSent) response.writeHead(502, { "content-type": "text/plain" });
    response.end("acceptance front: upstream unavailable\n");
  });
  request.pipe(upstream);
});

server.listen(LISTEN, "0.0.0.0", () => {
  process.stdout.write(`acceptance front listening on ${LISTEN}\n`);
});
