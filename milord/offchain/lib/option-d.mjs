// Option D (Anti-Snipe candidate, NOT an approved launch setting): the Team Safe owns the Hookr family through the
// Anti-Snipe window, then hands it to HookrFamilyLock. Every LP fee the founding position earned until the handover
// is paid to the Safe (HookrLauncher LPFeesCollected events). The Safe then sends 10% of the post-commission Snipe
// LP part to the Team Safe and everything else (base LP fees + 90% of the Snipe part) to BuybackBurner; the MILORD
// fees go to BuybackBurner and are burned. Pure functions only: no RPC, no keys (see offchain/option-d-reconcile.mjs).
//
// Per v4 Swap event of the MILORD pool (currency0 = native ETH, currency1 = MILORD):
//   buy  (amount0 < 0): poolIn = -amount0 ETH, LP fee = ceil(poolIn * fee / 1e6) ETH,
//                       Snipe LP part = LP fee * (fee - base) / fee   (0 when fee == base)
//   sell (amount1 < 0): LP fee = ceil(poolIn * fee / 1e6) MILORD
// The fee field is what the pool charged: base + 80% of the Snipe (Hookr's 20% never reaches the pool), so the
// Snipe LP part is already post-commission. Same formulas as test/fork/MilordForkBase._readSwapLog, which the VPS
// fork run checked against real Hookr-1 (handover payout = event sum within 12 wei).

export const PIPS = 1_000_000n;
const ceilDiv = (a, b) => (a + b - 1n) / b;
const pos = (l) => [BigInt(l.blockNumber), BigInt(l.logIndex)];
const le = (a, b) => a[0] < b[0] || (a[0] === b[0] && a[1] <= b[1]);

/**
 * swaps: [{ blockNumber, logIndex, txHash, amount0, amount1, fee }] (Swap events of the MILORD pool, any order)
 * collections: [{ blockNumber, logIndex, txHash, recipient, amount0, amount1 }] (LPFeesCollected of the family)
 * opts: { baseFeePips, guardEndBlock, handover: { blockNumber, logIndex }, recipients: [helper, safe],
 *         teamBps = 1000, tolerancePerSwap = 2n }
 * Returns the split, every row it used, and the checks. Throws on anything that would make the split wrong.
 */
export function reconcileOptionD(swaps, collections, opts) {
  const base = BigInt(opts.baseFeePips);
  const teamBps = BigInt(opts.teamBps ?? 1000);
  const end = pos(opts.handover);
  const ours = new Set(opts.recipients.map((a) => a.toLowerCase()));
  const rows = [];
  let estEth = 0n, estMilord = 0n, surcharge = 0n, guardSwaps = 0;
  for (const s of [...swaps].sort((a, b) => (le(pos(a), pos(b)) ? -1 : 1))) {
    if (!le(pos(s), end)) continue;
    const a0 = BigInt(s.amount0), a1 = BigInt(s.amount1), fee = BigInt(s.fee);
    const buy = a0 < 0n;
    const inGuard = BigInt(s.blockNumber) < BigInt(opts.guardEndBlock);
    if (fee < base) throw new Error(`swap ${s.txHash}: fee ${fee} below the base ${base}`);
    if (fee > base && !(buy && inGuard)) throw new Error(`swap ${s.txHash}: Snipe fee ${fee} outside a guard buy`);
    const poolIn = buy ? -a0 : -a1;
    const lpFee = ceilDiv(poolIn * fee, PIPS);
    const snipe = buy ? (lpFee * (fee - base)) / fee : 0n;
    if (buy) estEth += lpFee; else estMilord += lpFee;
    surcharge += snipe;
    if (inGuard) guardSwaps++;
    rows.push({ block: Number(s.blockNumber), logIndex: Number(s.logIndex), tx: s.txHash, side: buy ? "buy" : "sell", poolIn, fee: Number(fee), lpFee, snipeLp: snipe });
  }
  let gotEth = 0n, gotMilord = 0n;
  const used = [];
  for (const c of collections) {
    if (!le(pos(c), end)) continue;
    if (!ours.has(c.recipient.toLowerCase())) throw new Error(`LPFeesCollected ${c.txHash} paid ${c.recipient}, not the helper or the Safe`);
    gotEth += BigInt(c.amount0);
    gotMilord += BigInt(c.amount1);
    used.push({ block: Number(c.blockNumber), tx: c.txHash, recipient: c.recipient, eth: BigInt(c.amount0), milord: BigInt(c.amount1) });
  }

  // Checks. During the guard the founding position is the only liquidity, so for guard trades the pool's LP fee is
  // ours; after the guard outside LPs may share base fees, so what we collected can only be lower than the estimate.
  const tol = BigInt(opts.tolerancePerSwap ?? 2n) * BigInt(rows.length) + 10n;
  const checks = [];
  const check = (ok, what) => { checks.push({ ok, what }); if (!ok) throw new Error(`reconciliation failed: ${what}`); };
  check(gotEth <= estEth + tol, `collected ETH ${gotEth} <= event estimate ${estEth} + ${tol}`);
  check(gotMilord <= estMilord + tol, `collected MILORD ${gotMilord} <= event estimate ${estMilord} + ${tol}`);
  check(gotEth + tol >= surcharge, `collected ETH ${gotEth} covers the Snipe LP part ${surcharge}`);

  const toTeam = (surcharge * teamBps) / 10_000n;
  const toBurner = gotEth - toTeam;
  return {
    toTeam, toBurner, milordToBurn: gotMilord,
    surcharge, baseEth: gotEth - surcharge, collectedEth: gotEth, collectedMilord: gotMilord,
    estimate: { eth: estEth, milord: estMilord, toleranceWei: tol },
    swaps: rows.length, guardSwaps, rows, collections: used, checks,
  };
}

/** Safe Transaction Builder batch for the split (encoded only; never signed or sent here). */
export function optionDBatch({ chainId, safe, teamSafe, burner, milord, split, iface, createdAt = Date.now() }) {
  const tx = (to, value, data, description) => ({ to, value: String(value), data, contractMethod: null, contractInputsValues: null, description });
  const txs = [
    tx(burner, split.toBurner, "0x", "BuybackBurner: base LP fees + 90% of the post-commission Snipe LP part"),
    tx(teamSafe, split.toTeam, "0x", "Team Safe: 10% of the post-commission Snipe LP part"),
  ];
  if (split.milordToBurn > 0n) {
    txs.push(tx(milord, 0n, iface.erc20.encodeFunctionData("transfer", [burner, split.milordToBurn]), "MILORD LP fees to BuybackBurner"));
    txs.push(tx(burner, 0n, iface.burner.encodeFunctionData("burnHeldTokens"), "burn them"));
  }
  return {
    version: "1.0", chainId: String(chainId), createdAt,
    meta: {
      name: "MILORD Option D window split (review before signing)",
      description: `From ${safe}. Generated by offchain/option-d-reconcile.mjs from PoolManager Swap and HookrLauncher LPFeesCollected events. Snipe LP part ${split.surcharge} wei, team ${split.toTeam}, burner ${split.toBurner}, MILORD burned ${split.milordToBurn}.`,
    },
    transactions: txs,
  };
}
