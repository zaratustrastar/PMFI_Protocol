// Minimal entry-quote service.  node offchain/quote-server.mjs
//
// LOCAL MODE (default): signs with anvil test key #3 using LOCAL_WEI_PER_MILLION. For anvil only.
// PRODUCTION: replace `priceSource` with a TWAP read from the MILORD/ETH pool plus a second source and
// deviation bound, and load the signer from a KMS/HSM. No fallback price is ever used.
import http from "node:http";
import fs from "node:fs";
import { Wallet, hexlify, randomBytes, parseEther, JsonRpcProvider } from "ethers";
import { issueQuote } from "./lib/quote.mjs";

const D = JSON.parse(fs.readFileSync(new URL("../sim/deployments.local.json", import.meta.url), "utf8"));
if (D.chainId !== 31337) throw new Error("local quote server only runs against anvil");
const provider = new JsonRpcProvider(process.env.RPC_URL ?? "http://127.0.0.1:8545");
const signer = new Wallet("0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"); // anvil #3
const LOCAL_PRICE = parseEther(process.env.LOCAL_WEI_PER_MILLION ?? "0.03");
const priceSource = async () => ({ weiPerMillion: LOCAL_PRICE, observedAt: (await provider.getBlock("latest")).timestamp });

http.createServer(async (req, res) => {
  res.setHeader("access-control-allow-origin", "*");
  const u = new URL(req.url, "http://x");
  if (u.pathname !== "/quote") { res.writeHead(404).end(); return; }
  try {
    const account = u.searchParams.get("account");
    const now = (await provider.getBlock("latest")).timestamp;
    const { quote, signature } = await issueQuote({ signer, chainId: D.chainId, converter: D.MilordConverter, account, priceSource, now, randomNonce: hexlify(randomBytes(32)) });
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ quote: Object.fromEntries(Object.entries(quote).map(([k, v]) => [k, v.toString()])), signature }));
  } catch (e) {
    res.writeHead(503, { "content-type": "application/json" }).end(JSON.stringify({ error: String(e.message ?? e) }));
  }
}).listen(8787, () => console.log("quote service on http://127.0.0.1:8787/quote?account=0x…"));
