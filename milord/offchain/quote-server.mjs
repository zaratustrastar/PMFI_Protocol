// Entry-quote service.  node offchain/quote-server.mjs
//
// Price: offchain/lib/entry-price.mjs. 30-minute TWAP of the Hookr MILORD/ETH pool rebuilt from the complete
// PoolManager Initialize/Swap event history; quote = max(TWAP, spot). Bounded bootstrap in the first 30 minutes
// (highest price since open). No quote when stale, spot >10% from TWAP, spot outside the liquidity range or
// liquidity too low. There is no fallback price; redemption never depends on this service.
//
// LOCAL MODE (default): anvil only (sim/deployments.local.json), signs with anvil test key #3.
// DEPLOYED MODE: DEPLOYMENTS_FILE=<deployments/robinhood-<chain>.json completed by record-launch.mjs>, RPC_URL,
// QUOTE_SIGNER_PRIVATE_KEY (the converter's quote signer; KMS/HSM preferred), QUOTE_PORT, QUOTE_HOST. Robinhood
// mainnet (4663) is refused unless ALLOW_MAINNET=yes after owner approval, or ALLOW_LOCAL_FORK=yes with a loopback
// RPC (private fork). The wall-clock head check is on.
import http from "node:http";
import fs from "node:fs";
import { Wallet, hexlify, randomBytes, JsonRpcProvider, isAddress } from "ethers";
import { issueQuote } from "./lib/quote.mjs";
import { entryPriceSource } from "./lib/entry-price.mjs";
import { chainAllowed } from "./services/lib/config.mjs";

const file = process.env.DEPLOYMENTS_FILE ?? new URL("../sim/deployments.local.json", import.meta.url).pathname;
const D = JSON.parse(fs.readFileSync(file, "utf8"));
const local = Number(D.chainId) === 31337;
if (!local && !process.env.DEPLOYMENTS_FILE) throw new Error("set DEPLOYMENTS_FILE for a deployed chain");
if (!chainAllowed(D.chainId, process.env.RPC_URL ?? "http://127.0.0.1:8545")) throw new Error("Robinhood mainnet: set ALLOW_MAINNET=yes only after owner approval (ALLOW_LOCAL_FORK=yes accepts a loopback RPC only)");
for (const k of ["MilordConverter", "MILORD", "PoolManager", "StateView", "lpFee", "tickSpacing"]) if (D[k] === undefined) throw new Error(`deployments file lacks ${k} (run offchain/record-launch.mjs after the launch)`);
const provider = new JsonRpcProvider(process.env.RPC_URL ?? "http://127.0.0.1:8545", Number(D.chainId), { staticNetwork: true });
if (Number((await provider.getNetwork()).chainId) !== Number(D.chainId)) throw new Error("RPC chain id differs from the deployments file");
const signer = local
  ? new Wallet("0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6") // anvil #3
  : new Wallet(process.env.QUOTE_SIGNER_PRIVATE_KEY ?? (() => { throw new Error("set QUOTE_SIGNER_PRIVATE_KEY"); })());
// anvil only mines on demand; the UI test raises MAX_STALENESS. Anvil's clock is moved by tests, so the wall-clock
// head check is off locally and on (MAX_HEAD_LAG, default 120 s) everywhere else.
const source = entryPriceSource(provider, D, {
  maxStaleness: Number(process.env.MAX_STALENESS ?? 120),
  maxHeadLag: local ? Infinity : Number(process.env.MAX_HEAD_LAG ?? 120),
});

let lastError = null;
http.createServer(async (req, res) => {
  res.setHeader("access-control-allow-origin", "*");
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/healthz") {
    // up = the RPC answers and (outside anvil) its head is within MAX_HEAD_LAG of the wall clock. A refused quote
    // (price gap, stale TWAP) is an expected wait, not ill health; lastError shows it.
    try {
      const b = await provider.getBlock("latest");
      const age = Math.floor(Date.now() / 1000) - b.timestamp;
      const up = local || age <= Number(process.env.MAX_HEAD_LAG ?? 120);
      res.writeHead(up ? 200 : 503, { "content-type": "application/json" }).end(JSON.stringify({ up, head: b.number, headAgeSecs: age, lastError }));
    } catch (e) {
      res.writeHead(503, { "content-type": "application/json" }).end(JSON.stringify({ up: false, error: String(e.shortMessage ?? e.message ?? e) }));
    }
    return;
  }
  if (u.pathname !== "/quote") { res.writeHead(404).end(); return; }
  const account = u.searchParams.get("account");
  if (!isAddress(account)) { res.writeHead(400, { "content-type": "application/json" }).end(JSON.stringify({ error: "account must be an address" })); return; }
  try {
    const now = (await provider.getBlock("latest")).timestamp;
    const { quote, signature } = await issueQuote({
      signer, chainId: D.chainId, converter: D.MilordConverter, account, now, randomNonce: hexlify(randomBytes(32)),
      priceSource: () => source.price(), maxPriceAge: Number(process.env.MAX_STALENESS ?? 120),
    });
    lastError = null;
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ quote: Object.fromEntries(Object.entries(quote).map(([k, v]) => [k, v.toString()])), signature }));
  } catch (e) {
    lastError = { at: new Date().toISOString(), error: String(e.message ?? e) };
    res.writeHead(503, { "content-type": "application/json" }).end(JSON.stringify({ error: String(e.message ?? e), retryAfter: e.retryAfter ?? null }));
  }
}).listen(Number(process.env.QUOTE_PORT ?? 8787), process.env.QUOTE_HOST ?? "127.0.0.1", () => console.log(`quote service on ${process.env.QUOTE_HOST ?? "127.0.0.1"}:${process.env.QUOTE_PORT ?? 8787}/quote?account=0x…`));
