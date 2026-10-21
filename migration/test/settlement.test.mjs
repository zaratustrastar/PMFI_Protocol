import test from "node:test";
import assert from "node:assert/strict";
import { Interface, keccak256 } from "ethers";
import {
  reconcileSettlement,
  verifyDistributions,
} from "../src/settlement.mjs";
const a = "0x" + "1".repeat(40),
  b = "0x" + "2".repeat(40),
  hash = "0x" + "1".repeat(64);
const s = {
  ownerReviewed: true,
  reviewReference: "reviewed",
  milordToken: a,
  distributionSafe: a,
  milordCodeHash: keccak256("0x01"),
  milordDecimals: 18,
  proceedsAsset: "ETH wei",
  saleTransactions: [{ tx: hash, pmfiUnits: "100", proceedsUnits: "10" }],
  acquisitionTransactions: [
    { tx: hash, milordUnits: "50", proceedsSpentUnits: "9" },
  ],
  deductions: { proceedsUnits: "1", milordUnits: "0", reason: "gas" },
  pmfiRemainingUnits: "0",
  proceedsRemainingUnits: "0",
  netMilordUnits: "50",
};
test("settlement reconciles exact sale, acquisition, deductions and remaining units", () => {
  assert.equal(reconcileSettlement(s, 100n).netMilord, "50");
  assert.throws(
    () => reconcileSettlement({ ...s, netMilordUnits: "51" }, 100n),
    /reconcile/,
  );
  assert.throws(() => reconcileSettlement(s, 101n), /reconcile/);
});
test("distribution verification requires matching token, canonical finalized receipt, no duplicates or overpayment", async () => {
  const iface = new Interface([
      "event Transfer(address indexed from,address indexed to,uint256 value)",
    ]),
    event = iface.encodeEventLog("Transfer", [a, b, 50n]);
  const provider = {
    getNetwork: async () => ({ chainId: 4663 }),
    getCode: async () => "0x01",
    getBlock: async () => ({ number: 10, timestamp: 100, hash }),
    getTransactionReceipt: async () => ({
      status: 1,
      blockNumber: 5,
      blockHash: hash,
      logs: [{ index: 0, address: a, ...event }],
    }),
  };
  const manifest = {
      createdAt: 50,
      destinationChain: 4663,
      settlement: s,
      rows: [{ recipient: b, amount: "50" }],
    },
    refs = [{ tx: hash, logIndex: 0 }];
  assert.equal(
    (await verifyDistributions(provider, manifest, refs)).sent[b],
    true,
  );
  await assert.rejects(
    verifyDistributions(provider, manifest, [...refs, ...refs]),
    /Duplicate/,
  );
  await assert.rejects(
    verifyDistributions(
      { ...provider, getNetwork: async () => ({ chainId: 1 }) },
      manifest,
      refs,
    ),
    /chain/,
  );
  await assert.rejects(
    verifyDistributions(
      { ...provider, getBlock: async () => ({ number: 2, hash }) },
      manifest,
      refs,
    ),
    /finalized/,
  );
});
