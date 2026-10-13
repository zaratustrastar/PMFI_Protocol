# Hookr compatibility

Source: public Hookr documentation (hookr.fun/docs: launchpad, pools/fees, concepts/immutability-and-ownership,
guides/open-an-existing-asset-market, reference/config-schema-and-limits, reference/contracts-and-addresses),
read on 2026-10-13. Hookr contracts were not inspected on-chain (RPC blocked from this environment), so
every point below should be confirmed against the deployed source before any launch decision.

## Requirement being tested

MILORD needs a buy/sell tax of at most 1.5%, adjustable downward (emergency reductions) and within bounds,
routed ⅓ buyback-and-burn / ⅓ Team Safe / ⅓ Arbitrage Safe, with 0% on ordinary transfers and a
6,767,000,000 supply.

## Findings

| Hookr path | Supply | Tax editable? | Routes to project Safes? | Verdict |
|---|---|---|---|---|
| Native launch (instant) | fixed 1,000,000,000, no owner, no mint, no transfer tax; whole supply in a launchpad-owned locked position at a fixed 2.5 ETH FDV | no (Hook Blocks immutable once the pool opens) | no | **incompatible** (supply, ownership) |
| Native launch (auction) | fixed 1B; 20–50% of supply reserved | no | no | **incompatible** |
| "Open an existing-asset market" | any existing token, e.g. MILORD | **no**: burnBps, lpBps, potBps, royaltyBps and protocol share are frozen forever at open | partly: royalty ≤10% of LP/pot cuts; burn applies to exact-input buys only; Hookr protocol takes 20–50% of block fees; ETH pairs add a 0.3% flywheel fee and a 50/50 creator/protocol split of quote fees | **conflict** |

Additional problems with the existing-asset lane: anyone can open a market for any token (no ownership
check), the opener supplies no liquidity (pool starts empty), and guard-window / snipe protection is off.

**Conclusion:** Hookr cannot deliver an editable ≤1.5% buy/sell tax routed to the project Safes. Per the
brief, no production launch contract is implemented until you choose an alternative.

## Smallest viable alternatives

| Option | Code | Fee editable | Who receives the fee | Notes |
|---|---|---|---|---|
| **A. Plain Uniswap v4 pool, static 1.5% LP fee**, project provides the main (initially token-only) position | none | no (fixed at pool creation; a new pool would be needed to change it) | LPs pro-rata; the project captures it while it is the dominant in-range LP; collected fees go to `TradingTaxSplitter` by Safe tx | Simplest; cannot do emergency reductions; third-party LPs dilute capture. |
| **B. v4 pool with a minimal dynamic-fee hook** (only `updateDynamicLPFee`, cap 1.5%, reductions immediate, increases delayed) | ~60 lines, audit needed | yes, bounded | LPs pro-rata (same capture model as A) | Meets editability; still not a true tax. Smallest code that satisfies the spec. |
| C. Custom hook taking a hook fee via `beforeSwap`/`afterSwap` deltas to the splitter | larger, delta accounting | yes | the splitter directly (true tax) | Real tax; most risk; the brief says avoid custom hooks unless essential. |
| D. Hookr existing-asset lane with frozen parameters | none | no | mostly not to Safes | Contradicts the spec. |

**Recommendation: B**, because it is the least complex option that keeps the fee editable within bounds
(spec requirement) and needs no ERC-20 transfer changes. If editability is not essential at launch, A has no
code at all. In both, "tax" is economically an LP fee: it is only captured in proportion to the project's
share of in-range liquidity, and fees accrue in ETH on buys and in MILORD on sells (MILORD fees can go
straight to `BuybackBurner.burnHeldTokens`).

Robinhood Chain v4 deployments (from public docs, unverified on-chain): PoolManager
`0x8366a39CC670B4001A1121B8F6A443A643e40951`, PositionManager `0x58daec3116aae6D93017bAAea7749052E8a04fA7`,
Quoter `0x8dc178efb8111bb0973dd9d722ebeff267c98f94`, Universal Router `0x8876789976dEcBfCbBbe364623C63652db8C0904`,
Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`, WETH `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`.
