import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Store } from "../src/store.mjs";
import { sync } from "../src/chain.mjs";
import { health } from "../src/service.mjs";
import { clientAddress, createLimiter } from "../src/rate-limit.mjs";
import { acquireLedgerLock } from "../src/lock.mjs";
const a = "0x" + "1".repeat(40),
  safe = "0x" + "2".repeat(40),
  token = "0x" + "3".repeat(40);
const c = {
  mode: "local",
  chainId: 31337,
  recipientChainId: 4663,
  origin: "http://localhost",
  token,
  safe,
  confirmations: 1,
  startBlock: 1,
  start: "2026-01-01T00:00:00Z",
  end: "2027-01-01T00:00:00Z",
  maxIndexAgeSeconds: 120,
  decimals: 18,
};
test("responsive but stale RPC and future-dated blocks fail closed", async () => {
  for (const timestamp of [
    Math.floor(Date.now() / 1000) - 3600,
    Math.floor(Date.now() / 1000) + 100,
  ]) {
    const s = new Store(":memory:", c),
      blocks = [
        { number: 0, hash: "h0", timestamp },
        { number: 1, hash: "h1", parentHash: "h0", timestamp },
      ];
    const rpc = {
      verify: async () => true,
      head: async () => 1,
      block: async (n) => blocks[n],
      logs: async () => [],
      balance: async () => 0n,
    };
    await assert.rejects(sync(s, rpc), /stale or future/);
    assert.equal(health(s, c), false);
    s.close();
  }
});
test("gross incoming plus same-transaction Safe outflow is never credited as net", () => {
  const s = new Store(":memory:", c);
  s.set("head", 10);
  s.commit({ number: 1, hash: "h1", timestamp: 1767225601 }, [
    { tx: "tax", logIndex: 0, from: a, to: safe, amount: "100" },
    { tx: "tax", logIndex: 1, from: safe, to: token, amount: "10" },
  ]);
  assert.equal(s.deposits()[0].status, "accounting-review");
  assert.deepEqual(s.totals(), {});
  assert.equal(s.accountingIssues()[0].reason, "ambiguous-net-receipt");
  s.close();
});
test("collection refunds/outflows require reconciliation instead of silently recycling contributions", () => {
  const s = new Store(":memory:", c);
  s.commit({ number: 1, hash: "h", timestamp: 1767225601 }, [
    { tx: "refund", logIndex: 0, from: safe, to: a, amount: "1" },
  ]);
  assert.equal(s.accountingIssues()[0].reason, "outflow-during-collection");
  s.close();
});
test("Nginx clients have independent quotas; forwarding headers are trusted only from explicitly trusted loopback proxy", () => {
  const req = {
    socket: { remoteAddress: "127.0.0.1" },
    headers: { "x-real-ip": "198.51.100.1" },
  };
  assert.equal(clientAddress(req), "127.0.0.1");
  assert.equal(clientAddress(req, true), "198.51.100.1");
  assert.equal(
    clientAddress({ ...req, socket: { remoteAddress: "198.51.100.2" } }, true),
    "198.51.100.2",
  );
  const allow = createLimiter({ limit: 1, maxClients: 2 });
  assert.equal(allow("a", 0), true);
  assert.equal(allow("a", 1), false);
  assert.equal(allow("b", 1), true);
  assert.equal(allow("c", 1), false);
  assert.equal(allow("a", 60001), true);
});
test("exclusive ledger ownership prevents simultaneous service/CLI writers", () => {
  const dir = mkdtempSync(join(tmpdir(), "pmfi-lock-")),
    path = join(dir, "ledger");
  const release = acquireLedgerLock(path);
  assert.throws(() => acquireLedgerLock(path), /locked/);
  release();
  acquireLedgerLock(path)();
  rmSync(dir, { recursive: true });
});
