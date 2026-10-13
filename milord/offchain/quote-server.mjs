// Entry-quote service.  node offchain/quote-server.mjs
//
// Price: 30-minute TWAP of the MILORD/ETH pool built from PoolManager Initialize/Swap events (PoolPriceSource).
// No quote while the pool has under 30 minutes of history, while observations are stale, or while spot is more
// than 10% away from the TWAP. There is no fallback price.
//
// LOCAL MODE (default): anvil only, signs with anvil test key #3.
// PRODUCTION: load the signer from a KMS/HSM, point RPC_URL at a Robinhood Chain node, keep the defaults, and add
// a second price source (e.g. the HookrQuoter or an off-chain reference) with a deviation bound before signing.
import http from "node:http";
import fs from "node:fs";
import { Wallet, hexlify, randomBytes, JsonRpcProvider } from "ethers";
import { issueQuote } from "./lib/quote.mjs";
import { PoolPriceSource } from "./lib/pool-price.mjs";

const D = JSON.parse(fs.readFileSync(new URL("../sim/deployments.local.json", import.meta.url), "utf8"));
if (D.chainId !== 31337) throw new Error("local quote server only runs against anvil");
const provider = new JsonRpcProvider(process.env.RPC_URL ?? "http://127.0.0.1:8545");
const signer = new Wallet("0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"); // anvil #3
const source = new PoolPriceSource(provider, {
  poolManager: D.PoolManager, stateView: D.StateView,
  key: ["0x0000000000000000000000000000000000000000", D.MILORD, D.lpFee, D.tickSpacing, D.HookrTaxHook],
  window: 1800, minHistory: 1800, maxDeviationBps: 1000,
  maxStaleness: Number(process.env.MAX_STALENESS ?? 120), // anvil only mines on demand; the UI test raises this
});

http.createServer(async (req, res) => {
  res.setHeader("access-control-allow-origin", "*");
  const u = new URL(req.url, "http://x");
  if (u.pathname !== "/quote") { res.writeHead(404).end(); return; }
  try {
    const account = u.searchParams.get("account");
    const now = (await provider.getBlock("latest")).timestamp;
    const { quote, signature } = await issueQuote({
      signer, chainId: D.chainId, converter: D.MilordConverter, account, now, randomNonce: hexlify(randomBytes(32)),
      priceSource: () => source.price(), maxPriceAge: Number(process.env.MAX_STALENESS ?? 120),
    });
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ quote: Object.fromEntries(Object.entries(quote).map(([k, v]) => [k, v.toString()])), signature }));
  } catch (e) {
    res.writeHead(503, { "content-type": "application/json" }).end(JSON.stringify({ error: String(e.message ?? e) }));
  }
}).listen(8787, () => console.log("quote service on http://127.0.0.1:8787/quote?account=0x…"));
