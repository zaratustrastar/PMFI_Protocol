// node offchain/robinhood-clock-probe.mjs [hours=6] [samples=400]      (env RPC_URL, optional L1_RPC_URL)
//
// READ-ONLY probe of Robinhood Chain's clocks (eth_getBlockByNumber, eth_call; no keys, no transactions). Hookr counts
// the Anti-Snipe guard and every principal lock in block.number, which on an Arbitrum Orbit chain is the PARENT
// chain's height as the sequencer last saw it (Hookr assumes 12 s a step), while the Snipe decay runs on
// block.timestamp. An Anvil fork has neither property (block.number is the L2 height there), so the fork run proves
// the logic, not the wall-clock lengths. This probe measures, over recent history:
//   - seconds per parent block (block.number), and how unevenly it advances (largest jump, longest stall);
//   - how long 300 parent blocks (the candidate guard) and 7,200 (the temporary principal lock) actually took;
//   - with L1_RPC_URL (Ethereum mainnet): how far block.number lags the real L1 head, from L1 block timestamps;
//   - that ArbSys.arbBlockNumber() (0x64) equals the L2 block height (HookrClock's assumption).
import { JsonRpcProvider } from "ethers";

const rpc = process.env.RPC_URL;
if (!rpc) throw new Error("set RPC_URL (Robinhood Chain RPC; read-only calls only)");
const hours = Number(process.argv[2] ?? 6);
const samples = Number(process.argv[3] ?? 400);
const p = new JsonRpcProvider(rpc, undefined, { staticNetwork: false });
const chainId = Number((await p.getNetwork()).chainId);
const raw = (n) => p.send("eth_getBlockByNumber", [typeof n === "string" ? n : "0x" + n.toString(16), false]);
const head = await raw("latest");
const H = Number(head.number);
if (head.l1BlockNumber === undefined) throw new Error("this RPC returns no l1BlockNumber field: not an Arbitrum-style chain, or a non-Nitro node");

// L2 blocks per second near the head, to place the samples
const back = await raw(Math.max(0, H - 10_000));
const l2PerSec = (H - Number(back.number)) / Math.max(1, Number(head.timestamp) - Number(back.timestamp));
const span = Math.min(H, Math.round(hours * 3600 * l2PerSec));
const rows = [];
for (let i = 0; i <= samples; i++) {
  const b = await raw(H - span + Math.round((span * i) / samples));
  rows.push({ l2: Number(b.number), ts: Number(b.timestamp), l1: Number(b.l1BlockNumber) });
}

// ArbSys check (eth_call only)
const arb = await p.call({ to: "0x0000000000000000000000000000000000000064", data: "0xa3b1b31d", blockTag: "0x" + H.toString(16) }).catch((e) => `error: ${e.shortMessage ?? e.message}`);

const first = rows[0], last = rows[rows.length - 1];
const secPerL1 = (last.ts - first.ts) / Math.max(1, last.l1 - first.l1);
let maxJump = 0, maxStall = 0;
for (let i = 1; i < rows.length; i++) {
  maxJump = Math.max(maxJump, rows[i].l1 - rows[i - 1].l1);
  if (rows[i].l1 === rows[i - 1].l1) maxStall = Math.max(maxStall, rows[i].ts - rows[i - 1].ts);
}
const durations = (k) => {
  const out = [];
  for (let i = 0, j = 0; i < rows.length; i++) {
    while (j < rows.length && rows[j].l1 < rows[i].l1 + k) j++;
    if (j === rows.length) break;
    out.push(rows[j].ts - rows[i].ts);
  }
  out.sort((a, b) => a - b);
  return out.length ? { n: out.length, minMin: out[0] / 60, medianMin: out[out.length >> 1] / 60, maxMin: out[out.length - 1] / 60 } : null;
};

let l1Lag = null;
if (process.env.L1_RPC_URL) {
  const l1 = new JsonRpcProvider(process.env.L1_RPC_URL);
  const lags = [];
  for (const r of rows.filter((_, i) => i % Math.max(1, Math.floor(rows.length / 40)) === 0)) {
    const b = await l1.getBlock(r.l1);
    if (b) lags.push(r.ts - b.timestamp);
  }
  lags.sort((a, b) => a - b);
  l1Lag = { samples: lags.length, minSec: lags[0], medianSec: lags[lags.length >> 1], maxSec: lags[lags.length - 1] };
}

const out = {
  chainId, head: H, hours, samples: rows.length, l2BlocksPerSecond: +l2PerSec.toFixed(2),
  secondsPerParentBlock: +secPerL1.toFixed(3), hookrAssumes: 12,
  largestParentJumpBetweenSamples: maxJump, longestStallSecondsBetweenSamples: maxStall,
  sampleSpacingSeconds: +((last.ts - first.ts) / samples).toFixed(1),
  guard300: durations(300), lock7200: hours * 3600 / secPerL1 > 7200 ? durations(7200) : "window too short: rerun with hours >= 30",
  l1Lag, arbSysAtHead: typeof arb === "string" && arb.startsWith("0x") ? { value: Number(BigInt(arb)), l2Height: H, matches: Math.abs(Number(BigInt(arb)) - H) <= 1 } : arb,
};
console.log(JSON.stringify(out, null, 2));
