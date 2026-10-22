import test from "node:test";
import assert from "node:assert/strict";
import {
  allocate,
  eligibility,
  validateConfig,
  windowStatus,
} from "../src/core.mjs";
import { units, transferRequest, errorText } from "../public/wallet.mjs";
const a = "0x" + "1".repeat(40),
  b = "0x" + "2".repeat(40),
  safe = "0x" + "3".repeat(40);
const c = {
  safe,
  confirmations: 3,
  start: "2026-01-01T00:00:00Z",
  end: "2026-01-08T00:00:00Z",
};
test("cutoff is start-inclusive and end-exclusive; uses block time", () => {
  const e = {
    from: a,
    amount: "1",
    blockNumber: 8,
    timestamp: Date.parse(c.start) / 1000,
  };
  assert.equal(eligibility(c, e, 10), "eligible");
  assert.equal(eligibility(c, e, 9), "pending");
  assert.equal(
    eligibility(c, { ...e, timestamp: e.timestamp - 1 }, 10),
    "early-review",
  );
  assert.equal(
    eligibility(c, { ...e, timestamp: Date.parse(c.end) / 1000 }, 10),
    "late-review",
  );
  assert.equal(eligibility(c, { ...e, orphaned: true }, 10), "orphaned");
  assert.equal(eligibility(c, { ...e, from: safe }, 10), "review");
});
test("exact allocation, deterministic largest-remainder dust and huge values", () => {
  assert.deepEqual(allocate({ [b]: "1", [a]: "1" }, "3"), [
    { wallet: a, amount: "2" },
    { wallet: b, amount: "1" },
  ]);
  const pool = 2n ** 180n + 1n;
  const rows = allocate(
    { [a]: "9007199254740993123456789", [b]: "8" },
    pool.toString(),
  );
  assert.equal(
    rows.reduce((s, r) => s + BigInt(r.amount), 0n),
    pool,
  );
  assert.throws(() => allocate({}, "1"));
  assert.throws(() => allocate({ [a]: "-1" }, "1"));
  assert.throws(() => allocate({ [a]: "1" }, "1.1"));
});
test("safe parsing rejects rounding, exponent notation and negative input", () => {
  assert.equal(
    units("9007199254740993.123456789012345678", 18),
    9007199254740993123456789012345678n,
  );
  for (const s of ["1e18", "-1", "0", "NaN", "1,000", "0.0000001"])
    assert.throws(() => units(s, 6));
});
test("disabled defaults cannot enable transfers; no invented dates", () => {
  assert.equal(validateConfig({ mode: "disabled" }).mode, "disabled");
  assert.equal(windowStatus({ mode: "disabled" }), "Not scheduled");
  assert.throws(() => validateConfig({ mode: "live", chainId: 1 }));
});
test("wallet request enforces network/account/balance and sends only transfer with zero ETH", async () => {
  const s = { enabled: true, chainId: 8453, token: b, safe, decimals: 6 };
  const raw = {
    request: async ({ method }) => (method === "eth_chainId" ? "0x2105" : [a]),
  };
  const tx = await transferRequest(raw, s, a, "1.5", 2000000n);
  assert.equal(tx.to, b);
  assert.equal(tx.value, "0x0");
  assert.ok(tx.data.startsWith("0xa9059cbb"));
  await assert.rejects(
    transferRequest(raw, { ...s, enabled: false }, a, "1", 2000000n),
  );
  await assert.rejects(
    transferRequest(raw, { ...s, chainId: 1 }, a, "1", 2000000n),
  );
  await assert.rejects(transferRequest(raw, s, b, "1", 2000000n));
  await assert.rejects(transferRequest(raw, s, a, "3", 2000000n));
  assert.match(errorText({ code: 4001 }), /cancelled/);
  assert.match(errorText({ code: "INSUFFICIENT_FUNDS" }), /ETH/);
  assert.match(errorText({ code: "CALL_EXCEPTION" }), /reverted/);
});

test("owner-approved 1-of-1 Safe can be configured; invalid thresholds still fail closed", () => {
  const hash = "0x" + "a".repeat(64);
  const valid = {
    mode: "live",
    chainId: 8453,
    recipientChainId: 4663,
    origin: "https://pmfi.cc",
    token: a,
    safe,
    decimals: 18,
    tokenCodeHash: hash,
    safeCodeHash: hash,
    safeSingletonCodeHash: hash,
    owners: [b],
    threshold: 1,
    startBlock: 1,
    confirmations: 64,
    start: "2026-11-01T00:00:00Z",
    end: "2026-11-08T00:00:00Z",
    policyUrl: "https://pmfi.cc/migration-policy",
    tokenAccountingReview: "net-transfer-events-no-rebase",
    maxIndexAgeSeconds: 120,
  };
  assert.equal(validateConfig(valid), valid);
  for (const invalid of [
    { threshold: 0 },
    { threshold: 2 },
    { owners: [] },
    { owners: [b, b] },
    { token: safe },
  ]) {
    assert.throws(() => validateConfig({ ...valid, ...invalid }));
  }
  // Accepting an owner-selected signing threshold does not bypass the
  // independent production approval and RPC accounting health checks.
});
