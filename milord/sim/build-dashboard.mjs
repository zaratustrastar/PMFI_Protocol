// node sim/build-dashboard.mjs  ->  sim/output/dashboard.html (self-contained, data embedded)
import fs from "node:fs";
const root = new URL("..", import.meta.url);
const S = JSON.parse(fs.readFileSync(new URL("sim/output/simulation.json", root), "utf8"));
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
const n = (v, d = 4) => Number(v).toLocaleString("en-US", { maximumFractionDigits: d });

function bars(title, key, unit, d = 4) {
  const vals = S.weeks.map((w) => Number(w[key]));
  const max = Math.max(...vals, 1e-9);
  const W = 300, H = 140, bw = 60, gap = 30;
  const rects = vals.map((v, i) => {
    const h = Math.max(2, (v / max) * (H - 40));
    const x = 20 + i * (bw + gap);
    return `<rect x="${x}" y="${H - 20 - h}" width="${bw}" height="${h}" rx="3" class="bar"/><text x="${x + bw / 2}" y="${H - 24 - h}" class="v">${n(v, d)}</text><text x="${x + bw / 2}" y="${H - 4}" class="l">${esc(S.weeks[i].label)}</text>`;
  }).join("");
  return `<figure><figcaption>${esc(title)} <span>${esc(unit)}</span></figcaption><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${rects}</svg></figure>`;
}

const rows = [
  ["Milords awake", "activeNFTs", 0], ["Unique holders", "uniqueHolders", 0], ["Asleep (inventory)", "inventory", 0],
  ["Conversions (total)", "conversionsTotal", 0], ["Redemptions (total)", "redemptionsTotal", 0],
  ["Entry fees, ETH (total)", "ethEntryFeesTotal"], ["ETH to Arbitrage Safe (total)", "ethToArbitrageSafeTotal"], ["ETH to Team Safe (total)", "ethToTeamSafeTotal"],
  ["ETH bridged this week", "ethBridgedThisWeek"], ["USDC deposited this week", "usdcDepositedThisWeek", 2], ["Scenario P&L this week, USDC", "pnlUsdcThisWeek", 2],
  ["pARB minted this week", "parbMintedThisWeek"], ["pARB price, USDC", "ppsUsdc", 6], ["Reported assets, USDC", "reportedAssetsUsdc", 2],
  ["pARB allocated this week", "parbAllocatedThisWeek"], ["Dust carried", "dustCarried", 18], ["Eligible wallets", "eligibleWallets", 0],
  ["pARB claimed (total)", "claimedTotal"], ["MILORD supply", "milordSupply", 0], ["MILORD escrowed", "escrowMilord", 0],
  ["Burned on redeem (total)", "burnedOnExitTotal", 0], ["Burned by buyback (total)", "burnedByBuybackTotal", 0],
];
const table = `<table><thead><tr><th></th>${S.weeks.map((w) => `<th>${esc(w.label)}</th>`).join("")}</tr></thead><tbody>${
  rows.map(([l, k, d]) => `<tr><td>${esc(l)}</td>${S.weeks.map((w) => `<td>${n(w[k], d ?? 4)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
const people = Object.keys(S.weeks[0].tracked);
const tracked = `<table><thead><tr><th>Wallet</th>${S.weeks.map((w) => `<th>${esc(w.label)}<br><small>days held · allocated · cumulative</small></th>`).join("")}</tr></thead><tbody>${
  people.map((p) => `<tr><td>${esc(p.replace(/_/g, " "))}</td>${S.weeks.map((w) => { const t = w.tracked[p]; return `<td>${n(t.weightSeconds / 86400, 2)} · ${n(t.allocatedThisWeek)} · ${n(t.cumulative)}</td>`; }).join("")}</tr>`).join("")}</tbody></table>`;

const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MILORD simulation</title><style>
:root{--sky:#e0ebf4;--ink:#1e2238;--muted:#545d78;--line:#1e223829;--blue:#6895ce;--butter:#f4d35e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--sky:#151827;--ink:#e3e8f2;--muted:#9aa3bb;--line:#e3e8f229;--butter:#c9a93a}}
:root[data-theme="dark"]{--sky:#151827;--ink:#e3e8f2;--muted:#9aa3bb;--line:#e3e8f229;--butter:#c9a93a}
*{box-sizing:border-box;margin:0}body{background:var(--sky);color:var(--ink);font:15px/1.5 system-ui,sans-serif;padding:32px 16px}
main{max-width:1100px;margin:0 auto}h1{font-size:26px;letter-spacing:-.02em}h2{font-size:17px;margin:32px 0 10px}
.warn{background:var(--butter);color:#1e2238;border-radius:6px;padding:10px 14px;margin:14px 0;font-size:14px}
p,li{color:var(--muted);font-size:14px}.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
figure{border:1.5px solid var(--ink);border-radius:6px;padding:10px}figcaption{font-weight:600;font-size:14px}figcaption span{color:var(--muted);font-weight:400}
svg{width:100%;height:auto}.bar{fill:var(--blue)}.v{font-size:11px;text-anchor:middle;fill:var(--ink)}.l{font-size:11px;text-anchor:middle;fill:var(--muted)}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
small{color:var(--muted);font-weight:400}.ok{color:#2f7d4f}.bad{color:#b23b3b}code{font-size:12px}
</style></head><body><main>
<h1>MILORD local simulation</h1>
<div class="warn">${esc(S.disclaimer)}</div>
<p>Three UTC weeks on a local chain against the real contracts. Generated ${esc(S.generatedAt)}. Start: ${S.start.activeNFTs} Milords, no holders.</p>
<h2>Scenario inputs (hypothetical)</h2><ul>${Object.entries(S.scenario).map(([k, v]) => `<li><code>${esc(k)}</code>: ${esc(/Eth|wei/.test(k) ? `${Number(v) / 1e18} ETH` : /Bps/.test(k) ? `${Number(v) / 100}%` : k === "ethUsd" ? `$${v} per ETH (mock swap)` : v)}</li>`).join("")}</ul>
<h2>What happened each week</h2><ul>${S.weeks.map((w) => `<li><b>${esc(w.label)}</b>: ${esc(w.notes)}</li>`).join("")}</ul>
<h2>Charts</h2><div class="charts">${bars("Milords awake", "activeNFTs", "count", 0)}${bars("USDC deposited from fees", "usdcDepositedThisWeek", "USDC", 2)}${bars("pARB minted for holders", "parbMintedThisWeek", "pARB", 2)}${bars("pARB price", "ppsUsdc", "USDC per share", 6)}</div>
<h2>Weekly numbers</h2><div class="tw">${table}</div>
<h2>Tracked wallets</h2><p>Seller keeps week-1 earnings; buyer earns from the sale; redeemer stops earning at redemption; the smart wallet's allocation goes to its Base payout address.</p><div class="tw">${tracked}</div>
<h2>Checks</h2><ul>${S.checks.map((c) => `<li class="${c.pass ? "ok" : "bad"}">${c.pass ? "✓" : "✗"} ${esc(c.name)}</li>`).join("")}</ul>
</main></body></html>`;
fs.writeFileSync(new URL("sim/output/dashboard.html", root), html);
console.log("wrote sim/output/dashboard.html");
