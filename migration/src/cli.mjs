import { readFileSync, writeFileSync } from "node:fs";
import { validateConfig, fingerprint, allocate } from "./core.mjs";
import { Store } from "./store.mjs";
import { Chain, sync } from "./chain.mjs";
import { JsonRpcProvider } from "ethers";
import { reconcileSettlement, verifyDistributions } from "./settlement.mjs";
import { health } from "./service.mjs";
const c = validateConfig(
  JSON.parse(
    readFileSync(process.env.MIGRATION_CONFIG || "config.example.json", "utf8"),
  ),
);
const [command, arg] = process.argv.slice(2);
if (command === "fingerprint") {
  console.log(fingerprint(c));
  process.exit(0);
}
if (c.mode === "disabled")
  throw Error("Use a reviewed configuration or local test config");
const store = new Store(process.env.MIGRATION_DB || "migration.sqlite", c);
try {
  if (command === "index") {
    const chain = new Chain(
      (process.env.BASE_RPC_URLS || "").split(",").filter(Boolean),
      c,
    );
    try {
      await sync(store, chain);
    } finally {
      chain.providers.forEach((p) => p.destroy());
    }
  } else if (command === "export") {
    const bindings = store.db.prepare("SELECT * FROM bindings").all(),
      manifest = store.get("allocation")
        ? JSON.parse(store.get("allocation"))
        : null;
    const rows = store.deposits().map((e) => ({
      ...e,
      recipient: bindings.find((b) => b.wallet === e.from)?.recipient || null,
      walletAllocation:
        manifest?.rows.find((r) => r.wallet === e.from)?.amount || null,
      distributionTx:
        JSON.parse(store.get("distribution") || "null")
          ?.events.filter(
            (d) =>
              d.recipient ===
              bindings.find((b) => b.wallet === e.from)?.recipient,
          )
          .map((d) => d.tx)
          .join(";") || null,
    }));
    const report = {
      configHash: store.get("configHash"),
      config: c,
      healthy: health(store, c),
      asOf: store.tip(),
      reconciliation: JSON.parse(store.get("reconciliation") || "null"),
      totals: store.totals(),
      bindings,
      rows,
      manifest,
      allocationInvalid: store.get("allocationInvalid") === "true",
      distribution: JSON.parse(store.get("distribution") || "null"),
      audit: store.db.prepare("SELECT * FROM audit ORDER BY seq").all(),
    };
    if (!arg) throw Error("Export path prefix required");
    writeFileSync(arg + ".json", JSON.stringify(report, null, 2), {
      flag: "wx",
      mode: 0o600,
    });
    const cols = [
      "chainId",
      "token",
      "tx",
      "logIndex",
      "from",
      "to",
      "amount",
      "blockNumber",
      "blockHash",
      "timestamp",
      "status",
      "recipient",
      "walletAllocation",
      "distributionTx",
    ];
    const esc = (v) => '"' + String(v ?? "").replaceAll('"', '""') + '"';
    writeFileSync(
      arg + ".csv",
      [
        cols.join(","),
        ...rows.map((r) => cols.map((k) => esc(r[k])).join(",")),
      ].join("\n"),
      { flag: "wx", mode: 0o600 },
    );
  } else if (command === "allocate") {
    store.transaction(() => {
      if (
        !health(store, c) ||
        Date.now() < Date.parse(c.end) ||
        !c.bindingFrozen ||
        store.get("allocation")
      )
        throw Error(
          "Close window, sync, freeze bindings; allocation must not already exist",
        );
      const cutoffBlock = store.db
        .prepare(
          "SELECT number FROM blocks WHERE timestamp>=? ORDER BY number LIMIT 1",
        )
        .get(Date.parse(c.end) / 1000);
      if (
        !cutoffBlock ||
        Number(store.get("head")) - cutoffBlock.number + 1 < c.confirmations
      )
        throw Error("Cutoff is not confirmed");
      const settlement = JSON.parse(readFileSync(arg, "utf8"));
      if (
        settlement.ownerReviewed !== true ||
        !settlement.reviewReference ||
        !settlement.saleTransactions?.length ||
        !settlement.acquisitionTransactions?.length ||
        !settlement.deductions ||
        !/^\d+$/.test(settlement.netMilordUnits)
      )
        throw Error(
          "Reviewed settlement with proceeds, purchases, deductions and exact pool required",
        );
      const reconciliation = reconcileSettlement(
        settlement,
        Object.values(store.totals()).reduce((n, v) => n + BigInt(v), 0n),
      );
      const rows = allocate(store.totals(), settlement.netMilordUnits).map(
        (r) => {
          const binding = store.db
            .prepare("SELECT recipient FROM bindings WHERE wallet=?")
            .get(r.wallet);
          if (!binding) throw Error("Missing signed recipient for " + r.wallet);
          return { ...r, recipient: binding.recipient };
        },
      );
      const manifest = {
        version: 1,
        createdAt: Math.floor(Date.now() / 1000),
        configHash: store.get("configHash"),
        sourceBlock: store.tip(),
        destinationChain: 4663,
        settlement,
        reconciliation,
        rows,
        execution:
          "NOT EXECUTED; settlement amounts are owner-attested, independently verify before distribution",
      };
      store.set("allocation", JSON.stringify(manifest));
      store.audit("allocation-finalized", manifest);
      console.log(JSON.stringify(manifest, null, 2));
    });
  } else if (command === "verify-distribution") {
    if (!store.get("allocation") || store.get("allocationInvalid") === "true")
      throw Error("No valid allocation");
    if (!process.env.ROBINHOOD_RPC_URL)
      throw Error("ROBINHOOD_RPC_URL required");
    const provider = new JsonRpcProvider(
      process.env.ROBINHOOD_RPC_URL,
      undefined,
      { cacheTimeout: -1 },
    );
    try {
      const result = await verifyDistributions(
        provider,
        JSON.parse(store.get("allocation")),
        JSON.parse(readFileSync(arg, "utf8")),
      );
      store.transaction(() => {
        if (store.get("allocationInvalid") === "true")
          throw Error("Allocation invalidated during verification");
        store.set("distribution", JSON.stringify(result));
        store.audit("distribution-verified", result);
      });
      console.log(JSON.stringify(result, null, 2));
    } finally {
      provider.destroy();
    }
  } else
    throw Error(
      "Commands: fingerprint, index, export <prefix>, allocate <reviewed-settlement.json>, verify-distribution <events.json>",
    );
} finally {
  store.close();
}
