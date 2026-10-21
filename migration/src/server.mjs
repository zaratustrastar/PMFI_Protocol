import http from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { Store } from "./store.mjs";
import { Chain, sync } from "./chain.mjs";
import { validateConfig, address } from "./core.mjs";
import { publicState, issueChallenge, bind } from "./service.mjs";
export function createServer(c, store, chain, approval = null) {
  const rates = new Map();
  return http.createServer(async (req, res) => {
    const send = (code, data) => {
      res.writeHead(code, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      });
      res.end(JSON.stringify(data));
    };
    try {
      const url = new URL(req.url, "http://localhost");
      if (req.method === "GET" && url.pathname === "/api/migration/state")
        return send(200, publicState(store, c, approval));
      if (req.method === "GET" && url.pathname === "/api/migration/history") {
        const wallet = address(url.searchParams.get("wallet"));
        const rows = store ? store.deposits(wallet) : [],
          binding =
            store?.db
              .prepare(
                "SELECT recipient,verifiedAt FROM bindings WHERE wallet=?",
              )
              .get(wallet) || null;
        const invalid = store?.get("allocationInvalid") === "true";
        const distribution = store?.get("distribution")
          ? JSON.parse(store.get("distribution"))
          : null;
        const manifest =
          !invalid && store?.get("allocation")
            ? JSON.parse(store.get("allocation"))
            : null;
        return send(200, {
          healthy: publicState(store, c, approval).healthy,
          rows,
          binding,
          total: rows
            .filter((e) => e.status === "eligible")
            .reduce((s, e) => s + BigInt(e.amount), 0n)
            .toString(),
          allocation:
            manifest?.rows.find((r) => r.wallet === wallet)?.amount || null,
          distribution: invalid
            ? "Needs reconciliation"
            : distribution?.sent[binding?.recipient]
              ? "Sent"
              : manifest
                ? "Allocated"
                : "Not started",
          distributionEvents:
            distribution?.events.filter(
              (e) => e.recipient === binding?.recipient,
            ) || [],
        });
      }
      if (
        req.method === "POST" &&
        ["/api/migration/challenge", "/api/migration/bind"].includes(
          url.pathname,
        )
      ) {
        if (c.mode === "disabled" || !chain || !store)
          return send(503, { error: "Migration is not configured" });
        if (
          req.headers.origin !== c.origin ||
          !req.headers["content-type"]?.startsWith("application/json")
        )
          return send(403, { error: "Origin/content type rejected" });
        const key = req.socket.remoteAddress,
          now = Date.now();
        for (const [k, v] of rates) if (now - v.at > 60000) rates.delete(k);
        const rate = rates.get(key) || { at: now, n: 0 };
        rates.set(key, rate);
        if (++rate.n > 30)
          return send(429, { error: "Please wait before retrying" });
        let data = "";
        for await (const chunk of req) {
          data += chunk;
          if (data.length > 12000)
            return send(413, { error: "Request too large" });
        }
        const body = JSON.parse(data);
        if (url.pathname.endsWith("/challenge"))
          return send(
            200,
            issueChallenge(store, c, body.wallet, body.recipient),
          );
        return send(200, await bind(store, c, chain, body));
      }
      const files = {
        "/migrate": "index.html",
        "/migrate/": "index.html",
        "/migrate/app.js": "app.js",
        "/migrate/styles.css": "styles.css",
        "/migrate/privy-bridge.bundle.js": "privy-bridge.bundle.js",
      };
      const file = files[url.pathname];
      if (req.method === "GET" && file) {
        const path = resolve(
          fileURLToPath(new URL("../dist/", import.meta.url)),
          file,
        );
        if (!existsSync(path))
          return send(404, { error: "Build asset unavailable" });
        res.writeHead(200, {
          "Content-Type": file.endsWith(".html")
            ? "text/html; charset=utf-8"
            : file.endsWith(".css")
              ? "text/css"
              : "text/javascript",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          "Referrer-Policy": "strict-origin-when-cross-origin",
          "Content-Security-Policy":
            "frame-ancestors 'self'; object-src 'none'; base-uri 'self'",
        });
        return res.end(readFileSync(path));
      }
      return send(404, { error: "Not found" });
    } catch (e) {
      return send(400, { error: e.message?.slice(0, 250) || "Request failed" });
    }
  });
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const c = validateConfig(
    JSON.parse(
      readFileSync(
        process.env.MIGRATION_CONFIG || "config.example.json",
        "utf8",
      ),
    ),
  );
  const approval = process.env.MIGRATION_APPROVAL
    ? JSON.parse(readFileSync(process.env.MIGRATION_APPROVAL, "utf8"))
    : null;
  const store =
    c.mode === "disabled"
      ? null
      : new Store(process.env.MIGRATION_DB || "migration.sqlite", c);
  const chain =
    c.mode === "disabled"
      ? null
      : new Chain(
          (process.env.BASE_RPC_URLS || "").split(",").filter(Boolean),
          c,
        );
  // One indexer, one local database. No browser-provided hashes or amounts are accepted.
  let stopped = false;
  const loop = async () => {
    if (stopped) return;
    try {
      await sync(store, chain);
    } catch (e) {
      store.set("healthy", false);
      console.error("Indexer stopped accepting deposits:", e.message);
    }
    if (!stopped) setTimeout(loop, 5000).unref();
  };
  if (chain) void loop();
  const server = createServer(c, store, chain, approval);
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.listen(Number(process.env.PORT || 8787), "127.0.0.1", () =>
    console.log(
      "Migration preview: http://127.0.0.1:" +
        (process.env.PORT || 8787) +
        "/migrate",
    ),
  );
  process.on("SIGTERM", () => {
    stopped = true;
    server.close(() => process.exit(0));
  });
}
