import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { Store } from "../src/store.mjs";
const a = "0x" + "1".repeat(40),
  b = "0x" + "2".repeat(40),
  token = "0x" + "3".repeat(40),
  hash = "0x" + "1".repeat(64);
test("CLI freezes a reconciled allocation and exports CSV/JSON without broadcasting", () => {
  const dir = mkdtempSync(join(tmpdir(), "pmfi-cli-"));
  const config = join(dir, "config.json"),
    db = join(dir, "ledger.sqlite"),
    settlement = join(dir, "settlement.json"),
    prefix = join(dir, "export");
  const c = {
    mode: "local",
    chainId: 31337,
    recipientChainId: 4663,
    origin: "http://localhost",
    safe: b,
    token,
    decimals: 18,
    owners: [a, b],
    threshold: 2,
    tokenCodeHash: hash,
    safeCodeHash: hash,
    safeSingletonCodeHash: hash,
    startBlock: 1,
    confirmations: 2,
    start: "2026-01-01T00:00:00Z",
    end: "2026-01-02T00:00:00Z",
    policyUrl: "https://pmfi.cc/policy",
    tokenAccountingReview: "net-transfer-events-no-rebase",
    maxIndexAgeSeconds: 120,
    bindingFrozen: true,
  };
  writeFileSync(config, JSON.stringify(c));
  let s = new Store(db, c);
  s.commit({ number: 1, hash, timestamp: 1767225600 }, [
    { tx: hash, logIndex: 0, from: a, to: b, amount: "100" },
  ]);
  s.commit({ number: 2, hash, timestamp: 1767312000 }, []);
  s.set("head", 3);
  s.set("lastSync", Date.now());
  s.set("healthy", true);
  s.db
    .prepare("INSERT INTO bindings VALUES (?,?,?,?,?)")
    .run(a, a, "test-nonce", "test-signature", new Date().toISOString());
  s.close();
  writeFileSync(
    settlement,
    JSON.stringify({
      ownerReviewed: true,
      reviewReference: "local test",
      milordToken: token,
      milordCodeHash: hash,
      milordDecimals: 18,
      distributionSafe: b,
      proceedsAsset: "ETH wei",
      saleTransactions: [{ tx: hash, pmfiUnits: "100", proceedsUnits: "10" }],
      acquisitionTransactions: [
        { tx: hash, milordUnits: "50", proceedsSpentUnits: "10" },
      ],
      deductions: { proceedsUnits: "0", milordUnits: "0" },
      pmfiRemainingUnits: "0",
      proceedsRemainingUnits: "0",
      netMilordUnits: "50",
    }),
  );
  const run = (...args) =>
    spawnSync(process.execPath, ["src/cli.mjs", ...args], {
      encoding: "utf8",
      env: { ...process.env, MIGRATION_CONFIG: config, MIGRATION_DB: db },
    });
  let result = run("allocate", settlement);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).rows[0].amount, "50");
  assert.notEqual(run("allocate", settlement).status, 0);
  result = run("export", prefix);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(
    JSON.parse(readFileSync(prefix + ".json")).rows[0].walletAllocation,
    "50",
  );
  assert.match(readFileSync(prefix + ".csv", "utf8"), /"100"/);
  assert.notEqual(run("export", prefix).status, 0);
  s = new Store(db, c);
  s.rollback(0);
  assert.equal(s.get("allocationInvalid"), "true");
  s.close();
  rmSync(dir, { recursive: true });
});
