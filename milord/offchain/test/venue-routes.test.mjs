import test from "node:test";
import assert from "node:assert/strict";
import { checkVenueRoutes, feeBps, httpClient, TOKENS } from "../lib/venue-routes.mjs";

const ctx = { safe: "0x" + "11".repeat(20), servicer: "0x" + "22".repeat(20) };
function http({ feePips = 10, polyFeePips, noUsdg = false, acrossDown = false, relayDown = false, noAcrossRoute = false } = {}) {
  const calls = [];
  // fee in bps, to 0.01 bps
  const less = (n, bps = feePips) => String(BigInt(n) - (BigInt(n) * BigInt(Math.round(bps * 100))) / 1_000_000n);
  return {
    calls,
    get: async (u) => {
      calls.push(["GET", u]);
      if (u.endsWith("/supported-assets")) return { supportedAssets: [
        ...(noUsdg ? [] : [{ chainId: "4663", token: { address: TOKENS.usdg, decimals: 6 }, minCheckoutUsd: 2 }]),
        { chainId: "137", token: { address: TOKENS.pusd, decimals: 6 } },
      ] };
      if (acrossDown) throw new Error("HTTP 503");
      if (u.includes("/available-routes") && noAcrossRoute) return [];
      if (u.includes("/available-routes")) return u.includes("originChainId=4663")
        ? [{ originToken: TOKENS.usdg, destinationToken: TOKENS.baseUsdc }]
        : [{ originToken: TOKENS.baseUsdc, destinationToken: TOKENS.usdg }];
      if (u.includes("/swap/approval")) return { minOutputAmount: less(new URL(u).searchParams.get("amount")), swapTx: { to: "0x" + "33".repeat(20) } };
      throw new Error("unexpected " + u);
    },
    post: async (u, b) => {
      calls.push(["POST", u, b]);
      if (u.endsWith("/quote") && b.fromAmountBaseUnit) {
        const pips = typeof polyFeePips === "function" ? polyFeePips(b) : polyFeePips ?? feePips;
        return { estToTokenBaseUnit: less(b.fromAmountBaseUnit, pips), estCheckoutTimeMs: 60000 };
      }
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

// ── HTTP transport (regression: VPS run, Across 400 "POST method required when request.body is provided") ──
function fakeFetch() {
  const seen = [];
  const reply = (status, obj) => ({ ok: status < 300, status, text: async () => (typeof obj === "string" ? obj : JSON.stringify(obj)) });
  const f = async (url, init) => {
    const h = Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
    seen.push({ url, method: init.method, headers: h, body: init.body });
    // Across's observed rule: a GET that looks like it carries a body is refused
    if (init.method === "GET" && (init.body !== undefined || h["content-type"])) return reply(400, "POST method required when request.body is provided");
    if (url.includes("/swap/approval")) {
      const a = BigInt(new URL(url).searchParams.get("amount"));
      return reply(200, { expectedOutputAmount: String(a - a / 1000n), minOutputAmount: String(a - a / 500n), swapTx: { to: "0x" + "33".repeat(20) } });
    }
    if (url.includes("/available-routes")) return reply(200, url.includes("originChainId=4663")
      ? [{ originToken: TOKENS.usdg, destinationToken: TOKENS.baseUsdc }] : [{ originToken: TOKENS.baseUsdc, destinationToken: TOKENS.usdg }]);
    if (url.endsWith("/supported-assets")) return reply(200, { supportedAssets: [
      { chainId: "4663", token: { address: TOKENS.usdg, decimals: 6 } }, { chainId: "137", token: { address: TOKENS.pusd, decimals: 6 } }] });
    if (url.endsWith("/quote") && init.method === "POST") {
      const b = JSON.parse(init.body);
      if (b.fromAmountBaseUnit) return reply(200, { estToTokenBaseUnit: String(BigInt(b.fromAmountBaseUnit) * 9990n / 10000n) });
      return reply(400, "unsupported");
    }
    return reply(404, "not found");
  };
  return { f, seen };
}

test("httpClient: GET has no body and no content-type; POST sends JSON with its content type", async () => {
  const { f, seen } = fakeFetch();
  const h = httpClient(f);
  await h.get("https://app.across.to/api/swap/approval?amount=10000000");
  await h.post("https://bridge.polymarket.com/quote", { fromAmountBaseUnit: "10000000" });
  assert.equal(seen[0].method, "GET");
  assert.equal(seen[0].body, undefined);
  assert.equal(seen[0].headers["content-type"], undefined);
  assert.equal(seen[1].method, "POST");
  assert.equal(seen[1].headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(seen[1].body), { fromAmountBaseUnit: "10000000" });
  await assert.rejects(h.get("https://x.example/missing"), /HTTP 404/);
});

test("Across quotes succeed through the real transport (the 2b4bb4b transport got HTTP 400 on every quote)", async () => {
  const { f, seen } = fakeFetch();
  const r = await checkVenueRoutes(httpClient(f), ctx);
  const across = r.quotes.filter((q) => q.route.startsWith("Across"));
  assert.equal(across.length, 6);
  assert.deepEqual(r.summary.apiErrors, []);
  assert.ok(across.every((q) => q.feeBps === 20 && q.expectedOutRaw)); // policy on minOutputAmount, expected reported
  assert.ok(seen.filter((s) => s.url.includes("/swap/approval")).every((s) => s.method === "GET" && !s.headers["content-type"]));
  // the old transport's headers reproduce the refusal
  const old = await f("https://app.across.to/api/swap/approval?amount=1", { method: "GET", headers: { "content-type": "application/json" } });
  assert.equal(old.status, 400);
});

test("failures are classified: unavailable route, API error, fee policy", async () => {
  const noRoute = await checkVenueRoutes(http({ noAcrossRoute: true }), ctx);
  assert.equal(noRoute.ok, false);
  assert.deepEqual(noRoute.summary.routesUnavailable, ["Across route USDG(4663)->USDC(Base)", "Across route USDC(Base)->USDG(4663)"]);
  assert.deepEqual(noRoute.summary.feePolicyFailures, []);

  const down = await checkVenueRoutes(http({ acrossDown: true }), ctx);
  assert.ok(down.summary.apiErrors.length > 0 && down.summary.apiErrors.every((w) => /^Across/.test(w)));
  assert.deepEqual(down.summary.routesUnavailable, []);

  const pricey = await checkVenueRoutes(http({ feePips: 80 }), ctx);
  assert.equal(pricey.ok, false);
  assert.deepEqual(pricey.summary.routesUnavailable, []);
  assert.deepEqual(pricey.summary.apiErrors, []);
  assert.ok(pricey.summary.feePolicyFailures.length > 0);
});

test("observed Polymarket fees vs the 50 bps per-leg policy: deposits >= $100 pass, recalls fail; the bound is not raised", async () => {
  // VPS quotes: $10 60.25/80.55 bps, $100 25.84/58.43, $1,000 22.30/56.20 (deposit/recall)
  const observed = { "10000000": [6025, 8055], "100000000": [2584, 5843], "1000000000": [2230, 5620] }; // pips of 0.01 bps
  const polyFeePips = (b) => observed[b.fromAmountBaseUnit][b.fromChainId === "4663" ? 0 : 1] / 100;
  const r = await checkVenueRoutes(http({ polyFeePips: (b) => polyFeePips(b) }), ctx);
  assert.equal(r.summary.maxFeeBps, 50);
  assert.deepEqual(r.summary.routesUnavailable, []);
  assert.deepEqual(r.summary.feePolicyFailures.filter((w) => /^Polymarket/.test(w)), [
    "Polymarket deposit USDG(4663)->pUSD $10: fee <= 50 bps",
    "Polymarket recall pUSD->USDG(4663) $10: fee <= 50 bps",
    "Polymarket recall pUSD->USDG(4663) $100: fee <= 50 bps",
    "Polymarket recall pUSD->USDG(4663) $1000: fee <= 50 bps",
  ]);
  assert.ok(r.quotes.filter((q) => q.route.startsWith("Polymarket")).every((q) => q.preferred === true));
});

test("a malformed quote is an API error, not a fee result", async () => {
  const h = http();
  const post = h.post;
  h.post = async (u, b) => (u.endsWith("/quote") && b.fromAmountBaseUnit ? { estToTokenBaseUnit: "abc" } : post(u, b));
  const r = await checkVenueRoutes(h, ctx);
  assert.ok(r.summary.apiErrors.some((w) => /Polymarket deposit .*no plausible output/.test(w)));
});
