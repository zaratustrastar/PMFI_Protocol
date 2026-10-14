// node sim/build-dashboard.mjs  ->  sim/output/dashboard.html (self-contained, data embedded)
import fs from "node:fs";
const root = new URL("..", import.meta.url);
const S = JSON.parse(fs.readFileSync(new URL("sim/output/simulation.json", root), "utf8"));
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
const n = (v, d = 4) => Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const last = S.weeks.at(-1);
const R = S.reconciliation;

function bars(title, key, unit, d = 4) {
  const vals = S.weeks.map((w) => Number(w[key]));
  const max = Math.max(...vals.map(Math.abs), 1e-9);
  const W = 300, H = 150, bw = 60, gap = 30;
  const rects = vals.map((v, i) => {
    const h = Math.max(2, (Math.abs(v) / max) * (H - 44));
    const x = 20 + i * (bw + gap);
    return `<rect x="${x}" y="${H - 20 - h}" width="${bw}" height="${h}" rx="3" class="bar"/><text x="${x + bw / 2}" y="${H - 24 - h}" class="v">${n(v, d)}</text><text x="${x + bw / 2}" y="${H - 4}" class="l">${esc(S.weeks[i].label)}</text>`;
  }).join("");
  return `<figure><figcaption>${esc(title)} <span>${esc(unit)}</span></figcaption><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${rects}</svg></figure>`;
}

function line(title, pts, unit, d = 6) {
  const W = 300, H = 150, pad = 26;
  const ys = pts.map((p) => p.y), lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || hi || 1;
  const X = (i) => pad + (i * (W - 2 * pad)) / Math.max(1, pts.length - 1);
  const Y = (v) => H - pad - ((v - lo) / span) * (H - 2 * pad - 10);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
  const dots = pts.map((p, i) => `<circle cx="${X(i)}" cy="${Y(p.y)}" r="3" class="dot"><title>${esc(p.label)}: ${n(p.y, d)}</title></circle><text x="${X(i)}" y="${Y(p.y) - 7}" class="v">${n(p.y, d)}</text>`).join("");
  const labels = pts.map((p, i) => `<text x="${X(i)}" y="${H - 6}" class="l">${esc(p.short ?? "")}</text>`).join("");
  return `<figure><figcaption>${esc(title)} <span>${esc(unit)}</span></figcaption><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}"><path d="${path}" class="ln"/>${dots}${labels}</svg></figure>`;
}

const tiles = [
  ["MILORD supply", n(last.milordSupply, 0), `${n(last.milordBurnedTotal, 0)} burned`],
  ["Escrowed in converter", n(last.milordEscrowed, 0), `${last.activeNFTs} Milords × 1,000,000`],
  ["Milords", `${last.activeNFTs} awake`, `${last.inventoryNFTs} in inventory · ${last.totalMinted} IDs used`],
  ["ETH trading volume", `${n(last.ethTradingVolumeTotal, 3)} ETH`, "buys + sells through the pool"],
  ["Hookr commission", `${n(R.hookrCommission, 4)} ETH`, "20% of the 1.5% tax"],
  ["Team Safe revenue", `${n(last.teamRevenueEthTotal, 4)} ETH`, `+ LP fees ${n(R.lpFeesEth, 4)} ETH, ${n(R.lpFeesMilord, 0)} MILORD`],
  ["Arbitrage funding", `${n(R.arbitrageEth, 4)} ETH`, `→ ${n(R.usdgDeposited, 2)} USDG deposited`],
  ["pARB minted", n(R.parbMinted, 2), `${n(R.parbClaimed, 2)} claimed · ${n(last.unclaimed, 2)} unclaimed`],
  ["Vault NAV", `${n(last.navUsdg, 2)} USDG`, `PPS ${n(last.ppsUsdg, 6)}`],
];
const tilesHtml = `<div class="tiles">${tiles.map(([a, b, c]) => `<div class="tile"><div class="k">${esc(a)}</div><div class="big">${esc(b)}</div><div class="s">${esc(c)}</div></div>`).join("")}</div>`;

const rows = [
  ["MILORD supply", "milordSupply", 0], ["Circulating (outside escrow and pool)", "milordCirculating", 0], ["Escrowed", "milordEscrowed", 0], ["In the pool", "milordInPool", 0],
  ["Burned on redeem (total)", "burnedOnExitTotal", 0], ["Burned by buyback (total)", "burnedByBuybackTotal", 0],
  ["Milords awake", "activeNFTs", 0], ["Inventory", "inventoryNFTs", 0], ["Conversions (total)", "conversionsTotal", 0], ["Redemptions (total)", "redemptionsTotal", 0],
  ["ETH volume (total)", "ethTradingVolumeTotal"], ["Hookr commission (total)", "hookrCommissionTotal"], ["Project tax (total)", "projectTaxTotal"],
  ["Entry fees (total)", "ethEntryFeesTotal"], ["Royalties received (total)", "royaltiesTotal"], ["Team Safe revenue (total)", "teamRevenueEthTotal"],
  ["Arbitrage funding ETH (total)", "arbitrageFundingEthTotal"], ["Burn budget ETH (total)", "burnBudgetEthTotal"],
  ["ETH swapped to USDG this week", "ethSwappedThisWeek"], ["USDG deposited this week", "usdgDepositedThisWeek", 2], ["SCENARIO P&L this week, USDG", "pnlUsdgThisWeek", 2],
  ["pARB minted this week", "parbMintedThisWeek"], ["pARB price (PPS), USDG", "ppsUsdg", 6], ["Vault NAV, USDG", "navUsdg", 2],
  ["pARB allocated this week", "parbAllocatedThisWeek"], ["Eligible wallets", "eligibleWallets", 0], ["pARB claimed (total)", "claimedTotal"], ["pARB unclaimed", "unclaimed"],
  ["MILORD price, ETH per 1M (spot)", "weiPerMillionSpot", 6],
];
const table = `<table><thead><tr><th></th>${S.weeks.map((w) => `<th>${esc(w.label)}</th>`).join("")}</tr></thead><tbody>${
  rows.map(([l, k, d]) => `<tr><td>${esc(l)}</td>${S.weeks.map((w) => `<td>${n(w[k], d ?? 4)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;

const flows = [
  ["Taxed ETH volume", R.ethVolume, "ETH"], ["Buy tax (1.5%)", R.taxBuy, "ETH"], ["Sell tax (1.5%)", R.taxSell, "ETH"],
  ["→ Hookr commission (20%)", R.hookrCommission, "ETH"], ["→ project tax to TradingTaxSplitter", R.projectTax, "ETH"],
  ["   → buyback-and-burn budget", R.taxToBurner, "ETH"], ["   → Team Safe", R.taxToTeam, "ETH"], ["   → Arbitrage Funding Safe", R.taxToArbitrage, "ETH"],
  ["Entry fees", R.entryFees, "ETH"], ["   → Arbitrage Funding Safe (70%)", R.entryToArbitrage, "ETH"], ["   → Team Safe (30%)", R.entryToTeam, "ETH"],
  ["Royalties actually paid", R.royalties, "ETH"], ["LP fees (locked position, separate from tax)", R.lpFeesEth, "ETH"], ["LP fees in MILORD", R.lpFeesMilord, "MILORD"],
  ["Burn budget received", R.burnBudgetEth, "ETH"], ["Burn budget spent on buybacks", R.burnSpentEth, "ETH"], ["MILORD bought and burned", R.milordBurnedByBuyback, "MILORD"], ["MILORD burned on redemptions", R.milordBurnedOnExit, "MILORD"],
  ["Arbitrage funding ETH", R.arbitrageEth, "ETH"], ["ETH swapped to USDG", R.ethSwappedToUsdg, "ETH"], ["USDG deposited into pArbitrage V3", R.usdgDeposited, "USDG"],
  ["pARB minted to ShareDistributor", R.parbMinted, "pARB"], ["pARB allocated in roots", R.parbAllocated, "pARB"], ["pARB claimed", R.parbClaimed, "pARB"], ["pARB not yet allocated (dust/new)", R.parbUnallocated, "pARB"],
];
const flowTable = `<table><tbody>${flows.map(([l, v, u]) => `<tr><td>${esc(l)}</td><td>${n(v, 6)}</td><td>${esc(u)}</td></tr>`).join("")}</tbody></table>`;

const people = Object.keys(S.weeks[0].tracked);
const tracked = `<table><thead><tr><th>Wallet</th>${S.weeks.map((w) => `<th>${esc(w.label)}<br><small>days held · allocated · cumulative</small></th>`).join("")}<th>pARB balance now</th></tr></thead><tbody>${
  people.map((p) => `<tr><td>${esc(p.replace(/_/g, " "))}</td>${S.weeks.map((w) => { const t = w.tracked[p]; return `<td>${n(t.weightSeconds / 86400, 2)} · ${n(t.allocatedThisWeek)} · ${n(t.cumulative)}</td>`; }).join("")}<td>${n(last.tracked[p].pARBBalance)}</td></tr>`).join("")}</tbody></table>`;
const owners = `<table><thead><tr><th>Holder</th><th>Cumulative pARB</th><th>Claimed</th></tr></thead><tbody>${S.owners.map((o) => `<tr><td><code>${esc(o.address.slice(0, 10))}…</code></td><td>${n(o.cumulativePARB)}</td><td>${n(o.claimed)}</td></tr>`).join("")}</tbody></table>`;
const minFee = S.conversions.filter((c) => c.minimumApplied).length;

const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MILORD simulation</title><style>
:root{--sky:#e0ebf4;--ink:#1e2238;--muted:#545d78;--line:#1e223829;--blue:#6895ce;--butter:#f4d35e;--card:#ffffff99}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--sky:#151827;--ink:#e3e8f2;--muted:#9aa3bb;--line:#e3e8f229;--butter:#c9a93a;--card:#1e2238}}
:root[data-theme="dark"]{--sky:#151827;--ink:#e3e8f2;--muted:#9aa3bb;--line:#e3e8f229;--butter:#c9a93a;--card:#1e2238}
*{box-sizing:border-box;margin:0}body{background:var(--sky);color:var(--ink);font:15px/1.5 system-ui,sans-serif;padding:32px 16px}
main{max-width:1100px;margin:0 auto}h1{font-size:26px;letter-spacing:-.02em}h2{font-size:17px;margin:32px 0 10px}
.warn{background:var(--butter);color:#1e2238;border-radius:6px;padding:10px 14px;margin:14px 0;font-size:14px;font-weight:600}
p,li{color:var(--muted);font-size:14px}.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.tile{background:var(--card);border:1.5px solid var(--ink);border-radius:6px;padding:10px 12px}.k{font-size:12px;color:var(--muted)}.big{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}.s{font-size:12px;color:var(--muted)}
figure{border:1.5px solid var(--ink);border-radius:6px;padding:10px;background:var(--card)}figcaption{font-weight:600;font-size:14px}figcaption span{color:var(--muted);font-weight:400}
svg{width:100%;height:auto}.bar{fill:var(--blue)}.ln{fill:none;stroke:var(--blue);stroke-width:2}.dot{fill:var(--blue)}.v{font-size:10px;text-anchor:middle;fill:var(--ink)}.l{font-size:10px;text-anchor:middle;fill:var(--muted)}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left;white-space:normal}
small{color:var(--muted);font-weight:400}.ok{color:#2f7d4f}.bad{color:#b23b3b}code{font-size:12px}
details{margin-top:8px}summary{cursor:pointer;font-weight:600}
</style></head><body><main>
<h1>MILORD end-to-end simulation</h1>
<div class="warn">SIMULATED, NOT ACTUAL FINANCIAL PERFORMANCE. ${esc(S.disclaimer)}</div>
<p>One local chain configured like Robinhood Chain, against the real MILORD, pArbitrage V3 and Uniswap v4 contracts. Hookr's tax hook is a local mock with the same economics. Generated ${esc(S.generatedAt)}.</p>
<p><b>Illustrative launch (not approved):</b> ${esc(S.launch)}.</p>
<p><b>Start:</b> ${n(S.start.milordSupply, 0)} MILORD, ${n(S.start.milordInPool, 0)} in the position, ${n(S.start.milordOutsideLiquidity, 0)} held outside liquidity (${S.start.launchDustBurnedWei} wei rounding remainder burned), 0 ETH in the pool, 0 NFTs, 0 pARB. Opening price ${n(S.start.openingWeiPerMillion, 6)} ETH per 1M MILORD.</p>
<h2>Totals after three weeks (simulated)</h2>${tilesHtml}
<h2>Charts</h2><div class="charts">
${line("MILORD price", S.priceSeries.map((p, i) => ({ y: p.weiPerMillion, label: p.label, short: String(i + 1) })), "ETH per 1M MILORD (spot)", 4)}
${line("pARB price per share (PPS)", S.ppsSeries.map((p) => ({ y: p.pps, label: p.label, short: p.label.replace("week ", "w") })), "USDG", 6)}
${bars("Milords awake", "activeNFTs", "count", 0)}
${bars("USDG deposited from fees", "usdgDepositedThisWeek", "USDG", 2)}
${bars("pARB minted for holders", "parbMintedThisWeek", "pARB", 2)}
${bars("MILORD burned by buybacks (cumulative)", "burnedByBuybackTotal", "MILORD", 0)}
</div>
<p>Price points: ${S.priceSeries.map((p, i) => `${i + 1} ${esc(p.label)}`).join(" · ")}.</p>
<h2>Fee and asset reconciliation</h2><div class="tw">${flowTable}</div>
<h2>Weekly numbers</h2><div class="tw">${table}</div>
<h2>Entry fees</h2><p>${S.conversions.length} conversions. ${minFee} paid the 0.002 ETH minimum because 10% of the TWAP value of 1M MILORD was lower; ${S.conversions.length - minFee} paid 10%. Every quote came from a 30-minute TWAP; a quote requested 5 minutes after the market opened was refused.</p>
<h2>Tracked wallets</h2><p>The seller keeps week-1 earnings, the buyer earns from the sale, the redeemer stops earning at redemption and keeps what was earned.</p><div class="tw">${tracked}</div>
<h2>NFT owner allocations (top 15 by cumulative pARB)</h2><div class="tw">${owners}</div>
<h2>Steps</h2><details><summary>Show the transaction trace</summary><ol>${S.timeline.map((e) => `<li><b>${esc(e.step)}</b> ${esc(e.text)}${Object.keys(e).filter((k) => !["step", "t", "text"].includes(k)).map((k) => ` · ${esc(k)}: ${esc(typeof e[k] === "number" ? n(e[k]) : e[k])}`).join("")}</li>`).join("")}</ol></details>
<h2>What happened each week</h2><ul>${S.weeks.map((w) => `<li><b>${esc(w.label)}</b>: ${esc(w.notes)}</li>`).join("")}</ul>
<h2>Scenario inputs (hypothetical)</h2><ul>${Object.entries(S.scenario).map(([k, v]) => `<li><code>${esc(k)}</code>: ${esc(Array.isArray(v) ? v.map((x) => Number(x) / 1e18).join(", ") + " ETH" : /Eth/.test(k) ? `${Number(v) / 1e18} ETH` : /Bps/.test(k) ? `${Number(v) / 100}%` : v)}</li>`).join("")}</ul>
<h2>Checks (${S.checks.filter((c) => c.pass).length}/${S.checks.length} passed)</h2><ul>${S.checks.map((c) => `<li class="${c.pass ? "ok" : "bad"}">${c.pass ? "✓" : "✗"} ${esc(c.name)}${c.detail ? ` <small>(${esc(c.detail)})</small>` : ""}</li>`).join("")}</ul>
</main></body></html>`;
fs.writeFileSync(new URL("sim/output/dashboard.html", root), html);
console.log("wrote sim/output/dashboard.html");
