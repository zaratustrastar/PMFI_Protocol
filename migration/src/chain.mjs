import {
  JsonRpcProvider,
  Contract,
  Interface,
  id,
  keccak256,
  zeroPadValue,
  FetchRequest,
} from "ethers";
import { address } from "./core.mjs";
export const tokenABI = [
  "event Transfer(address indexed from,address indexed to,uint256 value)",
  "function balanceOf(address) view returns(uint256)",
  "function decimals() view returns(uint8)",
  "function symbol() view returns(string)",
  "function transfer(address,uint256) returns(bool)",
];
const iface = new Interface(tokenABI);
export class Chain {
  constructor(urls, c) {
    this.c = c;
    this.providers = urls.map((u) => {
      const request = new FetchRequest(u);
      request.timeout = 10000;
      return new JsonRpcProvider(request, undefined, { cacheTimeout: -1 });
    });
    if (!urls.length) throw Error("BASE_RPC_URLS required");
  }
  async call(fn) {
    let failure;
    for (let n = 0; n < 3; n++)
      for (const p of this.providers) {
        try {
          if (Number((await p.getNetwork()).chainId) !== this.c.chainId)
            throw Error("RPC chain mismatch");
          return await fn(p);
        } catch (e) {
          failure = e;
          await new Promise((r) => setTimeout(r, 100 * 2 ** n));
        }
      }
    throw Error("RPC request failed", { cause: failure });
  }
  block(n) {
    return this.call(async (p) => {
      const b = await p.getBlock(n);
      if (!b) throw Error("Block unavailable");
      return b;
    });
  }
  head() {
    return this.call((p) => p.getBlockNumber());
  }
  balance(n) {
    return this.call((p) =>
      new Contract(this.c.token, tokenABI, p).balanceOf(this.c.safe, {
        blockTag: n,
      }),
    );
  }
  async verify() {
    return this.call(async (p) => {
      const c = this.c;
      for (const [a, h] of [
        [c.token, c.tokenCodeHash],
        [c.safe, c.safeCodeHash],
      ]) {
        const code = await p.getCode(a);
        if (code === "0x" || keccak256(code) !== h)
          throw Error("Contract code differs from reviewed code");
      }
      const safe = new Contract(
        c.safe,
        [
          "function getOwners() view returns(address[])",
          "function getThreshold() view returns(uint256)",
          "function masterCopy() view returns(address)",
        ],
        p,
      );
      const [owners, threshold, singleton, decimals] = await Promise.all([
        safe.getOwners(),
        safe.getThreshold(),
        safe.masterCopy(),
        new Contract(c.token, tokenABI, p).decimals(),
      ]);
      if (keccak256(await p.getCode(singleton)) !== c.safeSingletonCodeHash)
        throw Error("Unreviewed Safe singleton");
      if (
        Number(decimals) !== c.decimals ||
        Number(threshold) !== c.threshold ||
        JSON.stringify(owners.map(address).sort()) !==
          JSON.stringify(c.owners.map(address).sort())
      )
        throw Error("Token decimals or Safe configuration mismatch");
      return true;
    });
  }
  async logs(block) {
    return this.call(async (p) => {
      const topic = zeroPadValue(this.c.safe, 32);
      const incoming = await p.getLogs({
        address: this.c.token,
        blockHash: block.hash,
        topics: [id("Transfer(address,address,uint256)"), null, topic],
      });
      const outgoing = await p.getLogs({
        address: this.c.token,
        blockHash: block.hash,
        topics: [id("Transfer(address,address,uint256)"), topic],
      });
      const unique = new Map(
        [...incoming, ...outgoing].map((l) => [
          `${l.transactionHash}:${l.index}`,
          l,
        ]),
      );
      const out = [];
      for (const l of unique.values()) {
        if (
          l.removed ||
          l.blockHash !== block.hash ||
          address(l.address) !== address(this.c.token)
        )
          throw Error("Invalid canonical log");
        const receipt = await p.getTransactionReceipt(l.transactionHash);
        if (
          !receipt ||
          receipt.status !== 1 ||
          receipt.blockHash !== block.hash ||
          !receipt.logs.some(
            (r) =>
              r.index === l.index &&
              r.address.toLowerCase() === l.address.toLowerCase() &&
              r.data === l.data &&
              JSON.stringify(r.topics) === JSON.stringify(l.topics),
          )
        )
          throw Error("Receipt/log mismatch");
        const e = iface.parseLog(l);
        out.push({
          tx: l.transactionHash,
          logIndex: l.index,
          from: address(e.args.from),
          to: address(e.args.to),
          amount: e.args.value.toString(),
        });
      }
      return out;
    });
  }
  async signature(wallet, message, signature) {
    return this.call(async (p) => {
      const { verifyMessage, hashMessage } = await import("ethers");
      if ((await p.getCode(wallet)) === "0x")
        return address(verifyMessage(message, signature)) === address(wallet);
      return (
        (await new Contract(
          wallet,
          ["function isValidSignature(bytes32,bytes) view returns(bytes4)"],
          p,
        ).isValidSignature(hashMessage(message), signature)) === "0x1626ba7e"
      );
    });
  }
}
export async function sync(store, chain, { limit = 500, now = Date.now } = {}) {
  store.set("healthy", false);
  await chain.verify();
  const c = store.c,
    head = await chain.head();
  let tip = store.tip();
  while (
    tip &&
    (tip.number > head || (await chain.block(tip.number)).hash !== tip.hash)
  ) {
    store.rollback(tip.number - 1);
    tip = store.tip();
  }
  // Baseline is tied to startBlock-1 hash: a deeper reorg requires an explicit full rebuild.
  const baselineBlock = await chain.block(c.startBlock - 1);
  if (
    store.get("baselineHash") &&
    store.get("baselineHash") !== baselineBlock.hash
  )
    throw Error("Reorg crossed baseline; rebuild required");
  if (!store.get("baselineHash"))
    store.transaction(() => {
      store.set("baselineHash", baselineBlock.hash);
    });
  if (store.get("baseline") === undefined)
    store.set("baseline", (await chain.balance(c.startBlock - 1)).toString());
  const start = tip ? tip.number + 1 : c.startBlock;
  for (let n = start; n <= Math.min(head, start + limit - 1); n++) {
    const b = await chain.block(n),
      previous = store.tip()?.hash || baselineBlock.hash;
    if (b.parentHash !== previous) throw Error("Chain changed during scan");
    const logs = await chain.logs(b);
    if ((await chain.block(n)).hash !== b.hash)
      throw Error("Chain changed during log read");
    store.commit(b, logs);
  }
  tip = store.tip();
  if (!tip) return;
  let expected = BigInt(store.get("baseline"));
  for (const e of store.events())
    if (!e.orphaned) {
      if (e.to === address(c.safe)) expected += BigInt(e.amount);
      if (e.from === address(c.safe)) expected -= BigInt(e.amount);
    }
  const actual = await chain.balance(tip.number);
  if ((await chain.block(tip.number)).hash !== tip.hash)
    throw Error("Chain changed during reconciliation");
  const observedAt = now();
  const chainAge = observedAt / 1000 - tip.timestamp;
  const fresh = chainAge >= -30 && chainAge <= c.maxIndexAgeSeconds;
  const accountingIssues = store.accountingIssues();
  store.transaction(() => {
    store.set("head", head);
    store.set(
      "reconciliation",
      JSON.stringify({
        block: tip.number,
        hash: tip.hash,
        expected: expected.toString(),
        actual: actual.toString(),
        matches: actual === expected,
      }),
    );
    store.set("lastSync", observedAt);
    store.set("chainTimestamp", tip.timestamp);
    store.set("accountingIssues", JSON.stringify(accountingIssues));
    store.set(
      "healthy",
      tip.number === head &&
        actual === expected &&
        fresh &&
        accountingIssues.length === 0,
    );
  });
  if (!fresh) throw Error("RPC canonical head is stale or future-dated");
  if (actual !== expected)
    throw Error(
      "Safe balance mismatch: pause and investigate token accounting",
    );
}
