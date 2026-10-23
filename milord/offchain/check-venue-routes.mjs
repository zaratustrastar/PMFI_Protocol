// node offchain/check-venue-routes.mjs          (B9; READ-ONLY: route lists and price quotes only)
//
// Env (optional): POLY_SAFE / SERVICER (addresses used as quote recipients; defaults are inert placeholders),
// ARB_BRIDGE_MAX_FEE_BPS (50), SIZES_USD (10,100,1000). Prints JSON; exit 1 when a required route fails.
// Relay is the engine's fallback: its result is reported separately (fallbackOk) and does not fail the run.
// Kalshi is not queried: it documents no API for crypto deposit addresses or withdrawals (operator steps).
import { checkVenueRoutes, DEFAULTS } from "./lib/venue-routes.mjs";

const call = async (method, url, body) => {
  const r = await fetch(url, { method, headers: { "content-type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(20_000) });
  const text = await r.text();
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${text.slice(0, 200)}`);
  return JSON.parse(text);
};
const placeholder = "0x000000000000000000000000000000000000dEaD";
const out = await checkVenueRoutes(
  { get: (u) => call("GET", u), post: (u, b) => call("POST", u, b) },
  { safe: process.env.POLY_SAFE ?? placeholder, servicer: process.env.SERVICER ?? placeholder },
  {
    maxFeeBps: Number(process.env.ARB_BRIDGE_MAX_FEE_BPS ?? DEFAULTS.maxFeeBps),
    ...(process.env.SIZES_USD ? { sizesRaw: process.env.SIZES_USD.split(",").map((x) => BigInt(Math.round(Number(x) * 1e6))) } : {}),
  },
);
console.log(JSON.stringify({ at: new Date().toISOString(), ...out }, null, 2));
process.exitCode = out.ok ? 0 : 1;
