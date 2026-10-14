// Entry-quote service.  node offchain/quote-server.mjs
//
// Price: offchain/lib/entry-price.mjs. 30-minute TWAP of the Hookr MILORD/ETH pool rebuilt from the complete
// PoolManager Initialize/Swap event history; quote = max(TWAP, spot). Bounded bootstrap in the first 30 minutes
// (highest price since open). No quote when stale, spot >10% from TWAP, spot outside the liquidity range or
// liquidity too low. There is no fallback price; redemption never depends on this service.
//
// LOCAL MODE (default): anvil only, signs with anvil test key #3.
// PRODUCTION: load the signer from a KMS/HSM, point RPC_URL at a Robinhood Chain node, keep the defaults, and add
// a second price source (e.g. the HookrQuoter or an off-chain reference) with a deviation bound before signing.
import http from "node:http";
import fs from "node:fs";
import { Wallet, hexlify, randomBytes, JsonRpcProvider } from "ethers";
import { issueQuote } from "./lib/quote.mjs";
import { entryPriceSource } from "./lib/entry-price.mjs";

const D = JSON.parse(fs.readFileSync(new URL("../sim/deployments.local.json", import.meta.url), "utf8"));
if (D.chainId !== 31337) throw new Error("local quote server only runs against anvil");
const provider = new JsonRpcProvider(process.env.RPC_URL ?? "http://127.0.0.1:8545");
const signer = new Wallet("0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"); // anvil #3
// anvil only mines on demand; the UI test raises MAX_STALENESS
// anvil's clock is moved by tests, so the wall-clock head check is off locally
const source = entryPriceSource(provider, D, { maxStaleness: Number(process.env.MAX_STALENESS ?? 120), maxHeadLag: Infinity });

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
    res.writeHead(503, { "content-type": "application/json" }).end(JSON.stringify({ error: String(e.message ?? e), retryAfter: e.retryAfter ?? null }));
  }
}).listen(8787, () => console.log("quote service on http://127.0.0.1:8787/quote?account=0x…"));
