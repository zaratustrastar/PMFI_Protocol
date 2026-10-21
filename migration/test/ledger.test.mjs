import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Wallet } from "ethers";
import { Store } from "../src/store.mjs";
import { sync } from "../src/chain.mjs";
import {
  bind,
  issueChallenge,
  publicState,
  approved,
} from "../src/service.mjs";
import { fingerprint } from "../src/core.mjs";
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
  confirmations: 2,
  startBlock: 1,
  start: "2026-01-01T00:00:00Z",
  end: "2026-01-08T00:00:00Z",
  maxIndexAgeSeconds: 120,
  decimals: 18,
};
function fixture() {
  const blocks = [0, 1, 2, 3].map((number) => ({
    number,
    hash: "hash" + number,
    parentHash: "hash" + (number - 1),
    timestamp: 1767225600 + number,
  }));
  const chain = {
    verify: async () => true,
    head: async () => 3,
    block: async (n) => blocks[n],
    logs: async (b) =>
      b.number === 1
        ? [{ tx: "tx", logIndex: 0, from: a, to: safe, amount: "100" }]
        : [],
    balance: async (n) => (n ? 100n : 0n),
  };
  return { blocks, chain };
}
test("persistent idempotent backfill, multiple events, outbound reconciliation and reorg audit", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pmfi-"));
  const path = join(dir, "ledger.sqlite");
  let s = new Store(path, c);
  const { chain, blocks } = fixture();
  await sync(s, chain);
  assert.equal(s.deposits().length, 1);
  assert.equal(s.totals()[a], "100");
  s.close();
  s = new Store(path, c);
  await sync(s, chain);
  assert.equal(s.deposits().length, 1);
  blocks[3] = {
    number: 3,
    hash: "replacement3",
    parentHash: "hash2",
    timestamp: 1767225603,
  };
  chain.logs = async (b) =>
    b.number === 3
      ? [
          { tx: "tx2", logIndex: 0, from: a, to: safe, amount: "20" },
          { tx: "tx2", logIndex: 1, from: a, to: safe, amount: "10" },
          { tx: "tx3", logIndex: 0, from: safe, to: a, amount: "5" },
        ]
      : [];
  chain.balance = async (n) => (n === 0 ? 0n : n === 3 ? 125n : 100n);
  await sync(s, chain);
  assert.equal(s.deposits().length, 3);
  assert.equal(s.get("healthy"), "true");
  assert.equal(s.totals()[a], "100");
  blocks[2] = {
    number: 2,
    hash: "fork2",
    parentHash: "hash1",
    timestamp: 1767225602,
  };
  blocks[3] = {
    number: 3,
    hash: "fork3",
    parentHash: "fork2",
    timestamp: 1767225603,
  };
  chain.logs = async () => [];
  chain.balance = async (n) => (n ? 100n : 0n);
  await sync(s, chain);
  assert.equal(s.deposits().filter((e) => !e.orphaned).length, 1);
  assert.equal(s.deposits().filter((e) => e.orphaned).length, 2);
  assert.ok(s.db.prepare("SELECT * FROM audit").all().length >= 2);
  s.close();
  rmSync(dir, { recursive: true });
});
test("RPC failure never advances cursor and balance mismatch disables service", async () => {
  const s = new Store(":memory:", c),
    { chain } = fixture();
  chain.logs = async () => {
    throw Error("RPC offline");
  };
  await assert.rejects(sync(s, chain));
  assert.equal(s.tip(), undefined);
  assert.equal(s.get("healthy"), "false");
  chain.logs = async () => [];
  await assert.rejects(sync(s, chain), /balance mismatch/);
  assert.equal(s.get("healthy"), "false");
  s.close();
});
test("deep reorg across baseline fails closed; config drift rejected", async () => {
  const s = new Store(":memory:", c),
    { chain, blocks } = fixture();
  await sync(s, chain);
  blocks[0].hash = "changed";
  await assert.rejects(sync(s, chain), /baseline/);
  assert.equal(s.get("healthy"), "false");
  s.close();
  const dir = mkdtempSync(join(tmpdir(), "pmfi-")),
    p = join(dir, "x.sqlite");
  new Store(p, c).close();
  assert.throws(
    () => new Store(p, { ...c, end: "2026-01-09T00:00:00Z" }),
    /config changed/,
  );
  rmSync(dir, { recursive: true });
});
test("binding requires depositor authorization, recipient control and unused domain-bound nonce", async () => {
  const s = new Store(":memory:", c),
    wallet = Wallet.createRandom(),
    recipient = Wallet.createRandom();
  const chain = {
    signature: async (w, m, sig) => {
      const { verifyMessage } = await import("ethers");
      return verifyMessage(m, sig).toLowerCase() === w;
    },
  };
  const challenge = issueChallenge(s, c, wallet.address, recipient.address);
  assert.match(challenge.message, /Source chain: 31337/);
  assert.match(challenge.message, /Destination chain: 4663/);
  assert.match(challenge.message, /Origin: http:\/\/localhost/);
  const signature = await wallet.signMessage(challenge.message);
  await assert.rejects(
    bind(s, c, chain, { nonce: challenge.nonce, signature }),
    /Recipient/,
  );
  await bind(s, c, chain, {
    nonce: challenge.nonce,
    signature,
    recipientSignature: await recipient.signMessage(challenge.message),
  });
  await assert.rejects(
    bind(s, c, chain, { nonce: challenge.nonce, signature }),
    /used/,
  );
  const expired = issueChallenge(s, c, wallet.address, wallet.address);
  s.db.prepare("UPDATE nonces SET expires=0 WHERE nonce=?").run(expired.nonce);
  await assert.rejects(
    bind(s, c, chain, { nonce: expired.nonce, signature }),
    /Expired/,
  );
  assert.throws(
    () =>
      issueChallenge(
        s,
        { ...c, bindingFrozen: true },
        wallet.address,
        wallet.address,
      ),
    /frozen/,
  );
  s.close();
});
test("live state requires exact approval config hash and fresh reconciled index", () => {
  const live = { ...c, mode: "live" };
  assert.equal(approved(live, null), false);
  assert.equal(
    approved(live, {
      ownerApproved: true,
      configHash: fingerprint(live),
      reference: "owner-approval",
    }),
    true,
  );
  assert.equal(
    approved(
      { ...live, safe: a },
      {
        ownerApproved: true,
        configHash: fingerprint(live),
        reference: "owner-approval",
      },
    ),
    false,
  );
  const state = publicState(null, { mode: "disabled" }, null);
  assert.equal(state.enabled, false);
  assert.equal(state.total, null);
});
