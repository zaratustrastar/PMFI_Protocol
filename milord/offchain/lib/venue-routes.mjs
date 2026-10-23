// Read-only checks of the MILORD capital routes between Robinhood Chain USDG and the venues (blocker B9).
// Only GET /supported-assets, GET /available-routes and price QUOTES are requested: nothing is signed or sent,
// no deposit or withdrawal address is created (Polymarket asks not to pre-generate withdrawal addresses).
//
// Routes (official docs, 2026-10-23):
//   Polymarket deposit   USDG (4663) -> Polymarket bridge deposit address -> pUSD (Polygon)      docs.polymarket.com/trading/bridge/deposit
//   Polymarket recall    pUSD (Polygon) -> POST /withdraw address -> USDG (4663)                 docs.polymarket.com/trading/bridge/withdraw
//   Kalshi deposit       USDG (4663) -> Across/Relay -> USDC (Base) -> Kalshi deposit address    help.kalshi.com crypto deposits
//   Kalshi recall        Kalshi app withdrawal (USDC) -> Across/Relay -> USDG (4663)              no withdrawal-creation API documented
export const CHAINS = { robinhood: 4663, base: 8453, polygon: 137 };
export const TOKENS = {
  usdg: "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
  baseUsdc: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  pusd: "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
};
export const DEFAULTS = {
  polymarket: "https://bridge.polymarket.com",
  across: "https://app.across.to/api",
  relay: "https://api.relay.link",
  maxFeeBps: 50, // ARB_BRIDGE_MAX_FEE_BPS, the engine's bound
  sizesRaw: [10_000_000n, 100_000_000n, 1_000_000_000n], // $10 / $100 / $1,000
};
const lc = (a) => String(a ?? "").toLowerCase();

export function feeBps(inRaw, outRaw) {
  if (inRaw <= 0n || outRaw < 0n) return null;
  return Number(((inRaw - outRaw) * 1_000_000n) / inRaw) / 100;
}

/**
 * http: { get(url) -> json, post(url, body) -> json }; ctx: { safe, servicer } (any nonzero addresses work for quotes).
 * Every check has a kind, so a failure says what failed:
 *   route  the provider does not list the route (or the token)            -> the route is unavailable
 *   api    the request failed (HTTP error, timeout) or the reply is malformed -> retry / provider problem
 *   fee    a valid quote costs more than maxFeeBps on that leg            -> fee policy refuses it (never auto-raised)
 */
export async function checkVenueRoutes(http, ctx, opts = {}) {
  const o = { ...DEFAULTS, ...opts };
  const checks = [];
  const quotes = [];
  const check = (kind, ok, what, detail) => { checks.push({ kind, ok: !!ok, what, ...(detail !== undefined ? { detail } : {}) }); return !!ok; };
  const tryStep = async (what, fn) => { try { return await fn(); } catch (e) { check("api", false, what, { error: String(e?.message ?? e).slice(0, 300) }); return null; } };
  const raw = (v) => { try { return v === undefined || v === null || v === "" ? null : BigInt(String(v)); } catch { return null; } };
  const quoteCheck = (route, inRaw, outRaw, extra = {}) => {
    const what = `${route} $${Number(inRaw) / 1e6}`;
    if (outRaw === null || outRaw <= 0n || outRaw > (inRaw * 101n) / 100n) return check("api", false, `${what}: quote has no plausible output`, { outRaw: String(outRaw) });
    const bps = feeBps(inRaw, outRaw);
    quotes.push({ route, inRaw: String(inRaw), outRaw: String(outRaw), feeBps: bps, withinPolicy: bps <= o.maxFeeBps, ...extra });
    return check("fee", bps <= o.maxFeeBps, `${what}: fee <= ${o.maxFeeBps} bps`, { outRaw: String(outRaw), feeBps: bps });
  };

  // 1. Polymarket bridge: assets
  const assets = await tryStep("Polymarket /supported-assets", () => http.get(`${o.polymarket}/supported-assets`));
  if (assets) {
    const list = assets.supportedAssets ?? [];
    const find = (chain, token) => list.find((a) => String(a.chainId) === String(chain) && lc(a.token?.address) === lc(token));
    const u = find(CHAINS.robinhood, TOKENS.usdg);
    check("route", u && Number(u.token.decimals) === 6, "Polymarket lists USDG (6 dec) on Robinhood Chain 4663", u ? { minCheckoutUsd: u.minCheckoutUsd } : undefined);
    check("route", find(CHAINS.polygon, TOKENS.pusd), "Polymarket lists pUSD on Polygon (collateral)");
  }
  // 2. Polymarket bridge: deposit and recall quotes
  for (const size of o.sizesRaw) {
    const dep = await tryStep(`Polymarket quote USDG->pUSD $${Number(size) / 1e6}`, () => http.post(`${o.polymarket}/quote`, {
      fromAmountBaseUnit: String(size), fromChainId: String(CHAINS.robinhood), fromTokenAddress: TOKENS.usdg,
      recipientAddress: ctx.safe, toChainId: String(CHAINS.polygon), toTokenAddress: TOKENS.pusd,
    }));
    if (dep) quoteCheck("Polymarket deposit USDG(4663)->pUSD", size, raw(dep.estToTokenBaseUnit), { etaMs: dep.estCheckoutTimeMs, preferred: true });
    const wd = await tryStep(`Polymarket quote pUSD->USDG $${Number(size) / 1e6}`, () => http.post(`${o.polymarket}/quote`, {
      fromAmountBaseUnit: String(size), fromChainId: String(CHAINS.polygon), fromTokenAddress: TOKENS.pusd,
      recipientAddress: ctx.servicer, toChainId: String(CHAINS.robinhood), toTokenAddress: TOKENS.usdg,
    }));
    if (wd) quoteCheck("Polymarket recall pUSD->USDG(4663)", size, raw(wd.estToTokenBaseUnit), { etaMs: wd.estCheckoutTimeMs, preferred: true });
  }
  // 3. Across: route list and swap quotes both ways (the engine's first provider)
  const routes = await tryStep("Across /available-routes", () => http.get(`${o.across}/available-routes?originChainId=${CHAINS.robinhood}&destinationChainId=${CHAINS.base}`));
  if (routes) check("route", Array.isArray(routes) && routes.some((r) => lc(r.originToken) === lc(TOKENS.usdg) && lc(r.destinationToken) === lc(TOKENS.baseUsdc)), "Across route USDG(4663)->USDC(Base)");
  const back = await tryStep("Across /available-routes (reverse)", () => http.get(`${o.across}/available-routes?originChainId=${CHAINS.base}&destinationChainId=${CHAINS.robinhood}`));
  if (back) check("route", Array.isArray(back) && back.some((r) => lc(r.originToken) === lc(TOKENS.baseUsdc) && lc(r.destinationToken) === lc(TOKENS.usdg)), "Across route USDC(Base)->USDG(4663)");
  for (const [name, from, fromTok, to, toTok] of [["USDG(4663)->USDC(Base)", CHAINS.robinhood, TOKENS.usdg, CHAINS.base, TOKENS.baseUsdc], ["USDC(Base)->USDG(4663)", CHAINS.base, TOKENS.baseUsdc, CHAINS.robinhood, TOKENS.usdg]]) {
    for (const size of o.sizesRaw) {
      const q = await tryStep(`Across quote ${name} $${Number(size) / 1e6}`, () => http.get(`${o.across}/swap/approval?` + new URLSearchParams({
        tradeType: "exactInput", amount: String(size), inputToken: fromTok, originChainId: String(from), outputToken: toTok,
        destinationChainId: String(to), depositor: ctx.servicer, recipient: ctx.servicer,
      })));
      // fee policy on the guaranteed minimum output; the expected output is reported alongside
      if (q) quoteCheck(`Across ${name}`, size, raw(q.minOutputAmount ?? q.expectedOutputAmount), {
        expectedOutRaw: q.expectedOutputAmount ?? null, swapTo: q.swapTx?.to ?? null,
      });
      const r = await tryStep(`Relay quote ${name} $${Number(size) / 1e6}`, () => http.post(`${o.relay}/quote`, {
        user: ctx.servicer, originChainId: from, destinationChainId: to, originCurrency: fromTok, destinationCurrency: toTok,
        amount: String(size), recipient: ctx.servicer, tradeType: "EXACT_INPUT",
      }));
      if (r) {
        const out = r.details?.currencyOut ?? {};
        quoteCheck(`Relay ${name}`, size, raw(out.minimumAmount ?? out.amount), { depositTo: r.steps?.at(-1)?.items?.[0]?.data?.to ?? null });
      }
    }
  }
  const required = checks.filter((c) => !/^Relay /.test(c.what)); // Relay is the engine's fallback, never required
  const failed = (kind) => required.filter((c) => c.kind === kind && !c.ok).map((c) => c.what);
  return {
    ok: required.length > 0 && required.every((c) => c.ok),
    summary: { routesUnavailable: failed("route"), apiErrors: failed("api"), feePolicyFailures: failed("fee"), maxFeeBps: o.maxFeeBps },
    fallbackOk: checks.filter((c) => /^Relay /.test(c.what)).every((c) => c.ok),
    checks, quotes,
  };
}

/**
 * The checker's HTTP transport. GET carries query parameters only: no body and no content-type header. The VPS run
 * of the first version sent `content-type: application/json` on GET, and Across's /swap/approval refused every
 * quote with HTTP 400 "POST method required when request.body is provided" (it treats a JSON content type as a
 * body; GET /swap/approval is the documented method, POST is only for embedded destination actions).
 * POST sends a JSON body with its content type. Any non-2xx reply or non-JSON body throws (an "api" failure).
 */
export function httpClient(fetchImpl = globalThis.fetch, timeoutMs = 20_000) {
  const call = async (method, url, body) => {
    const init = { method, headers: { accept: "application/json" }, signal: AbortSignal.timeout(timeoutMs) };
    if (method === "POST") {
      init.headers["content-type"] = "application/json";
      init.body = JSON.stringify(body ?? {});
    }
    const r = await fetchImpl(url, init);
    const text = await r.text();
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${text.slice(0, 200)}`);
    try { return JSON.parse(text); } catch { throw new Error(`non-JSON reply: ${text.slice(0, 120)}`); }
  };
  return { get: (u) => call("GET", u), post: (u, b) => call("POST", u, b) };
}
