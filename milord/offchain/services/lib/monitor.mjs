// Read-only monitor for the MILORD automation: no key, never sends a transaction. One pass reads the chain and the
// other services' health endpoints, and alerts on conditions that have persisted longer than their grace period.
// Each condition alerts once on onset (then throttled by the alerter) and sends "resolved" when it clears.
import { closedEpoch, rootInfo } from "./jobs.mjs";
import { EPOCH } from "../../lib/eligibility.mjs";

export const MONITOR_ABI = {
  vault: [
    "function lastReportTimestamp() view returns (uint256)",
    "function officialPPS() view returns (uint256)",
    "function paused() view returns (bool)",
    "function shutdown() view returns (bool)",
    "function totalPendingRedeemShares() view returns (uint256)",
    "function totalClaimableRedeemAssets() view returns (uint256)",
    "function idleBalance() view returns (uint256)",
  ],
  router: [
    "function paused() view returns (bool)",
    "function executable() view returns (uint256)",
    "function minSwapWei() view returns (uint256)",
    "function lastExecution() view returns (uint64)",
    "function priceReference() view returns (address)",
  ],
  reference: ["function assetPerEth() view returns (uint256)"],
  splitter: ["function undistributed(address) view returns (uint256)", "function totalOwed(address) view returns (uint256)"],
};

export const MONITOR_DEFAULTS = {
  navMaxAgeSecs: 2 * 3600, // the reporter signs at least hourly (MAX_REPORT_STALENESS); two missed reports alert
  ppsAlertBps: 100, // PPS move between two passes that alerts (warning); 5x this is critical
  feeStuckSecs: 6 * 3600, // ETH sitting in a splitter, the router or a tax queue this long
  settleStuckSecs: 24 * 3600, // a funded batch not settled into pARB (the vault processes deposits at each report)
  epochGraceSecs: 6 * 3600, // after an epoch's cutoff, before a missing root is alerted
  bridgeMaxSecs: 2 * 3600, // an in-transit transfer not confirmed after this
  minGasWei: 10n ** 16n, // 0.01 ETH per service key
  healthTimeoutMs: 5000,
};

/**
 * One monitoring pass. `c` holds read-only contracts (any may be null when not deployed yet):
 *   { provider, vault, router, reference?, splitters: [{name, contract}], adapter, distributor, taxQueues: [{name, queue}],
 *     lockWatch, wallets: [{name, address, minWei?}], health: [{name, url}], bridges?: async () => rows }
 * opts: { alerter, metrics, state: {read, write}, now: async () => unix secs, genesis, settlementGrace,
 *         watchLock: async (lockWatch) => void, fetchFn, ...MONITOR_DEFAULTS overrides }
 * Returns the list of active findings [{key, level, text}].
 */
export async function monitorTick(c, opts) {
  const o = { ...MONITOR_DEFAULTS, ...opts };
  const now = Number(await o.now());
  const st = o.state.read();
  st.onset ??= {};
  st.alerted ??= {};
  const active = [];
  const errors = [];

  // Condition helper: alert when `on` has held for `graceSecs` (0 = at once), "resolved" when it clears.
  const cond = async (key, on, level, text, graceSecs = 0) => {
    if (!on) {
      if (st.alerted[key]) await o.alerter.alert("resolved", `monitor:${key}`, `cleared: ${st.alerted[key]}`);
      delete st.onset[key];
      delete st.alerted[key];
      return;
    }
    st.onset[key] ??= now;
    if (now - st.onset[key] < graceSecs) return;
    active.push({ key, level, text });
    await o.alerter.alert(level, `monitor:${key}`, text);
    st.alerted[key] = text;
  };
  // A failing read is itself a finding (the RPC or a contract is broken); other checks still run.
  const check = async (name, fn) => {
    try {
      await fn();
      await cond(`read-${name}`, false);
    } catch (e) {
      errors.push(name);
      await cond(`read-${name}`, true, "warning", `monitor could not check ${name}: ${String(e?.shortMessage ?? e?.message ?? e)}`, 0);
    }
  };

  // 1. Gas on every service key (keeper, publisher, NAV reporter, servicer).
  for (const w of c.wallets ?? []) {
    await check(`gas-${w.name}`, async () => {
      const bal = BigInt(await c.provider.getBalance(w.address));
      const min = BigInt(w.minWei ?? o.minGasWei);
      o.metrics?.set(`gas_${w.name}_wei`, bal);
      await cond(`gas-${w.name}`, bal < min, bal < min / 4n ? "critical" : "warning",
        `${w.name} key ${w.address} has ${fmtEth(bal)} ETH, below ${fmtEth(min)}: its transactions will start failing`);
    });
  }

  // 2. Vault: missing NAV reports, unexpected PPS moves, paused/shutdown, redemptions waiting on liquidity.
  if (c.vault) await check("vault", async () => {
    const [lastTs, pps, paused, shutdown, pendingRedeem] = await Promise.all([
      c.vault.lastReportTimestamp(), c.vault.officialPPS(), c.vault.paused(), c.vault.shutdown(), c.vault.totalPendingRedeemShares(),
    ]);
    const age = now - Number(lastTs);
    o.metrics?.set("nav_report_age_secs", age);
    o.metrics?.set("official_pps", pps);
    await cond("nav-missing", Number(lastTs) > 0 && age > o.navMaxAgeSecs, age > 3 * o.navMaxAgeSecs ? "critical" : "warning",
      `no signed NAV report for ${Math.round(age / 60)} min (last ${new Date(Number(lastTs) * 1000).toISOString()}): deposits and redemptions are not being processed; check the NAV reporter`);
    await cond("vault-paused", paused || shutdown, "critical", `pARB vault is ${shutdown ? "SHUT DOWN" : "paused"}`);
    const prev = st.prevPps !== undefined ? BigInt(st.prevPps) : null;
    if (prev !== null && prev > 0n) {
      const moveBps = Number(((BigInt(pps) - prev) * 10_000n) / prev);
      o.metrics?.set("pps_move_bps", moveBps);
      const big = Math.abs(moveBps) > o.ppsAlertBps;
      await cond("pps-move", big, Math.abs(moveBps) > 5 * o.ppsAlertBps || moveBps < -o.ppsAlertBps ? "critical" : "warning",
        `pARB PPS moved ${moveBps > 0 ? "+" : ""}${(moveBps / 100).toFixed(2)}% since the previous pass (${prev} -> ${pps}): check the NAV inputs before the next report`);
    }
    st.prevPps = BigInt(pps).toString();
    st.pendingRedeemSince = BigInt(pendingRedeem) > 0n ? (st.pendingRedeemSince ?? now) : undefined;
    o.metrics?.set("pending_redeem_shares", pendingRedeem);
    await cond("redeem-waiting", BigInt(pendingRedeem) > 0n && now - (st.pendingRedeemSince ?? now) > o.settleStuckSecs, "warning",
      `${pendingRedeem} pARB shares have waited over ${o.settleStuckSecs / 3600} h for redemption: the engine must return liquidity to the vault`);
  });

  // 3. Fee path: splitters, the funding router and its price reference, the Hookr tax queues.
  for (const s of c.splitters ?? []) {
    await check(`splitter-${s.name}`, async () => {
      const [und, owed] = await Promise.all([s.contract.undistributed(ZERO), s.contract.totalOwed(ZERO)]);
      o.metrics?.set(`splitter_${s.name}_undistributed_wei`, und);
      await cond(`splitter-${s.name}-stuck`, BigInt(und) > 0n, "warning",
        `${s.name} splitter has held ${fmtEth(und)} undistributed ETH for over ${o.feeStuckSecs / 3600} h: the keeper is not distributing`, o.feeStuckSecs);
      await cond(`splitter-${s.name}-owed`, BigInt(owed) > 0n, "warning",
        `${s.name} splitter could not push ${fmtEth(owed)} ETH to a payee (credited as owed): that payee must call withdraw(0x0)`);
    });
  }
  if (c.router) await check("router", async () => {
    const [paused, exe, minWei, last, bal] = await Promise.all([
      c.router.paused(), c.router.executable(), c.router.minSwapWei(), c.router.lastExecution(), c.provider.getBalance(c.router.target ?? c.router.address),
    ]);
    o.metrics?.set("router_eth_wei", bal);
    await cond("router-paused", paused, "warning", `ArbitrageFundingRouter is paused holding ${fmtEth(bal)} ETH`, 0);
    const idle = !paused && BigInt(bal) >= BigInt(minWei) && now - Number(last) > o.feeStuckSecs;
    await cond("router-stuck", idle, "warning",
      `router holds ${fmtEth(bal)} ETH, last swap ${last > 0n ? `${Math.round((now - Number(last)) / 3600)} h ago` : "never"} (executable now: ${fmtEth(exe)}): check the keeper, the daily cap and the price reference`, o.feeStuckSecs);
    const ref = c.reference ?? null;
    if (ref) {
      let priceErr = null;
      try {
        const price = await ref.assetPerEth(); // not inside metrics?.set(...): optional chaining would skip the call
        o.metrics?.set("usdg_per_eth_e18", price);
      } catch (e) {
        priceErr = String(e?.shortMessage ?? e?.message ?? e);
      }
      // The router refuses to swap on a doubtful price; that is safe, but fees queue up while it lasts.
      await cond("price-stale", priceErr !== null, "warning", `funding price reference refuses a price (${priceErr}): the router will not swap`, 1800);
    }
  });
  for (const t of c.taxQueues ?? []) {
    await check(`tax-${t.name}`, async () => {
      const [ledger, incoming] = await Promise.all([t.queue.ledger(), t.queue.pending()]);
      const due = BigInt(ledger.creatorOwed) + BigInt(incoming);
      o.metrics?.set(`tax_${t.name}_due_wei`, due);
      await cond(`tax-${t.name}-stuck`, due > 0n, "warning",
        `Hookr ${t.name} tax queue has owed MILORD ${fmtEth(due)} ETH for over ${o.feeStuckSecs / 3600} h: the keeper's payCreator is not running or failing`, o.feeStuckSecs);
    });
  }

  // 4. Funding batches: ETH swapped to USDG but never settled into pARB (the vault has not processed the deposit).
  if (c.adapter) await check("adapter", async () => {
    const n = Number(await c.adapter.batchCount());
    let oldest = null;
    for (let i = n - 1; i >= Math.max(0, n - 20); i--) {
      const b = await c.adapter.batch(i);
      if (Number(b.settledAt) === 0 && Number(b.fundedAt) > 0) oldest = Math.min(oldest ?? Infinity, Number(b.fundedAt));
    }
    await cond("batch-unsettled", oldest !== null && now - oldest > o.settleStuckSecs, "warning",
      `a funding batch has waited ${Math.round((now - (oldest ?? now)) / 3600)} h to be settled into pARB: the vault needs a NAV report and the keeper's settle`);
  });

  // 5. Daily epochs. (a) A pending root whose review delay passed long ago and is still not active: the keeper's
  //    activateRoot step is not running (claims of the new days wait). (b) A closed day with shares to allocate and
  //    no root covering it, while nothing legitimately blocks the publisher: a pending root still in its 1-day review
  //    delay is expected (the publisher must wait for it), so the alert counts from the later of the day's cutoff and
  //    that root's activation time.
  if (c.distributor && o.genesis) await check("root-activation", async () => {
    const p = rootInfo(await c.distributor.pending());
    await cond("root-not-activated", p.set && now - Number(p.activatesAt) > o.epochGraceSecs, "warning",
      `the root for day ${p.epoch} could be activated ${Math.round((now - Number(p.activatesAt)) / 3600)} h ago and is still pending: check the keeper (activateRoot is permissionless)`);
  });
  if (c.distributor && c.adapter && o.genesis) await check("epoch", async () => {
    const k = closedEpoch(o.genesis, now - o.epochGraceSecs, o.settlementGrace);
    if (k === 0) return;
    const [active, pending] = (await Promise.all([c.distributor.active(), c.distributor.pending()])).map(rootInfo);
    const latest = Math.max(active.epoch, pending.set ? pending.epoch : 0);
    const cutoff = o.genesis + k * EPOCH + o.settlementGrace;
    const blockedUntil = pending.set ? Number(pending.activatesAt) : 0;
    let settled = 0n;
    const n = Number(await c.adapter.batchCount());
    for (let i = 0; i < n; i++) {
      const b = await c.adapter.batch(i);
      if (Number(b.settledAt) > 0 && Number(b.settledAt) < cutoff) settled += BigInt(b.sharesMinted);
    }
    const allocated = active.total > pending.total ? active.total : pending.total;
    o.metrics?.set("latest_root_epoch", latest);
    o.metrics?.set("days_behind", k - latest);
    await cond("epoch-missed", latest < k && settled > allocated && now - blockedUntil > o.epochGraceSecs, "critical",
      `day ${k} closed ${Math.round((now - cutoff) / 3600)} h ago with ${settled - allocated} unallocated pARB shares and no root covering it, and no pending root is in review: check the epoch publisher (it may be blocked by a vetoed root; shares roll forward only if no NFT was eligible)`);
  });

  // 6. Permanent-lock deadline (read-only; same check as the keeper, from an independent process).
  if (c.lockWatch && o.watchLock) await check("lock", () => o.watchLock(c.lockWatch));

  // 7. Bridges and venue transfers recorded by the arbitrage engine: in transit longer than allowed.
  if (c.bridges) await check("bridges", async () => {
    const rows = await c.bridges();
    const late = rows.filter((r) => !["confirmed", "completed", "failed_refunded"].includes(r.status) && now - Number(r.startedAt) > o.bridgeMaxSecs);
    o.metrics?.set("bridges_in_transit", rows.filter((r) => !["confirmed", "completed", "failed_refunded"].includes(r.status)).length);
    await cond("bridge-unconfirmed", late.length > 0, late.some((r) => now - Number(r.startedAt) > 4 * o.bridgeMaxSecs) ? "critical" : "warning",
      `${late.length} transfer(s) unconfirmed past ${o.bridgeMaxSecs / 3600} h: ${late.slice(0, 5).map((r) => `${r.id} ${r.kind ?? ""} ${r.amount ?? ""} (${r.status})`).join(", ")}`);
  });

  // 8. The other services: health endpoint up, and no new failed ticks (failed transactions surface here).
  for (const h of c.health ?? []) {
    let body = null;
    let err = null;
    try {
      const r = await (o.fetchFn ?? fetch)(h.url, { signal: AbortSignal.timeout(o.healthTimeoutMs) });
      body = await r.json().catch(() => ({}));
      if (!r.ok) err = `HTTP ${r.status}, last success ${Math.round((body?.lastSuccessAgeMs ?? 0) / 60000)} min ago`;
    } catch (e) {
      err = String(e?.message ?? e);
    }
    await cond(`health-${h.name}`, err !== null, "critical", `${h.name} is unhealthy: ${err}`);
    const fails = Number(body?.counters?.tick_failures ?? 0);
    const prevFails = Number(st.tickFailures?.[h.name] ?? fails);
    st.tickFailures = { ...(st.tickFailures ?? {}), [h.name]: fails };
    await cond(`failed-${h.name}`, fails > prevFails, "warning", `${h.name} had ${fails - prevFails} failed tick(s) since the last pass (failed transaction or RPC): see its logs`);
  }

  st.lastPass = now;
  o.state.write(st);
  o.metrics?.set("active_findings", active.length);
  return { active, errors };
}

const ZERO = "0x0000000000000000000000000000000000000000";
function fmtEth(wei) {
  return (Number(BigInt(wei)) / 1e18).toFixed(4);
}
