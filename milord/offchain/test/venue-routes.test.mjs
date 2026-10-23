import test from "node:test";
import assert from "node:assert/strict";
import { checkVenueRoutes, feeBps, TOKENS } from "../lib/venue-routes.mjs";

const ctx = { safe: "0x" + "11".repeat(20), servicer: "0x" + "22".repeat(20) };
function http({ feePips = 10, noUsdg = false, acrossDown = false, relayDown = false } = {}) {
  const calls = [];
  const less = (n) => String(BigInt(n) - (BigInt(n) * BigInt(feePips)) / 10_000n);
  return {
    calls,
    get: async (u) => {
      calls.push(["GET", u]);
      if (u.endsWith("/supported-assets")) return { supportedAssets: [
        ...(noUsdg ? [] : [{ chainId: "4663", token: { address: TOKENS.usdg, decimals: 6 }, minCheckoutUsd: 2 }]),
        { chainId: "137", token: { address: TOKENS.pusd, decimals: 6 } },
      ] };
      if (acrossDown) throw new Error("HTTP 503");
      if (u.includes("/available-routes")) return u.includes("originChainId=4663")
        ? [{ originToken: TOKENS.usdg, destinationToken: TOKENS.baseUsdc }]
        : [{ originToken: TOKENS.baseUsdc, destinationToken: TOKENS.usdg }];
      if (u.includes("/swap/approval")) return { minOutputAmount: less(new URL(u).searchParams.get("amount")), swapTx: { to: "0x" + "33".repeat(20) } };
      throw new Error("unexpected " + u);
    },
    post: async (u, b) => {
      calls.push(["POST", u, b]);
      if (u.endsWith("/quote") && b.fromAmountBaseUnit) return { estToTokenBaseUnit: less(b.fromAmountBaseUnit), estCheckoutTimeMs: 60000 };
      if (relayDown) throw new Error("HTTP 400 unsupported chain");
      return { details: { currencyOut: { minimumAmount: less(b.amount) } }, steps: [{ items: [{ data: { to: "0x" + "44".repeat(20) } }] }] };
    },
  };
}

test("all routes within the fee bound", async () => {
  const h = http();
  const r = await checkVenueRoutes(h, ctx);
  assert.equal(r.ok, true, JSON.stringify(r.checks.filter((c) => !c.ok)));
  assert.equal(r.fallbackOk, true);
  assert.equal(r.quotes.length, 3 * 2 + 3 * 2 * 2);
  // read-only: no /deposit or /withdraw address creation
  assert.ok(h.calls.every(([, u]) => !/\/(deposit|withdraw)$/.test(u)));
});

test("fee above the bound fails", async () => {
  const r = await checkVenueRoutes(http({ feePips: 80 }), ctx);
  assert.equal(r.ok, false);
});

test("USDG missing from Polymarket's assets fails", async () => {
  const r = await checkVenueRoutes(http({ noUsdg: true }), ctx);
  assert.equal(r.ok, false);
  assert.ok(r.checks.some((c) => !c.ok && /USDG/.test(c.what)));
});

test("Across outage fails; Relay outage is reported as fallback only", async () => {
  assert.equal((await checkVenueRoutes(http({ acrossDown: true }), ctx)).ok, false);
  const r = await checkVenueRoutes(http({ relayDown: true }), ctx);
  assert.equal(r.ok, true);
  assert.equal(r.fallbackOk, false);
});

test("feeBps", () => {
  assert.equal(feeBps(1_000_000n, 999_000n), 10);
  assert.equal(feeBps(0n, 1n), null);
});
