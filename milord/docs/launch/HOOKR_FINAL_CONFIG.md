# Hookr launch configuration

> **2026-10-14 final decisions (Bobo):** 100% of supply in the founding position, 0 ETH, ~10 ETH opening FDV, range
> top ~1,000×, 1.5%/1.5% tax with no launch surcharge, no transfer tax, no custom hook. Details: LAUNCH_LIQUIDITY.md.
> Still to approve: LP fee (0.3% proposed), commission (20% target), lock type, position owner and fee beneficiary
> (LP_LOCK.md).

Sources: hookr.fun docs read on 2026-10-13 (the-new-hookr, open-an-existing-asset-market, hooks/tax-conversion,
pools/fees, guides/providing-liquidity, pools/launch-lock, hooks/revenue-router, hooks/auto-burn, hooks/anti-snipe,
reference/contracts-and-addresses). Hookr's hookr-1 contracts were **not** read on-chain (the container's network
blocks the Robinhood RPC and hookr.fun); no hookr-1 ABI is published in the docs. Every item marked EXTERNAL must
be confirmed against the verified source on the explorer before a production transaction is generated.

## 1. What the docs say today, and what it changes

| Topic | Finding | Effect on the plan |
|---|---|---|
| Release | Docs mix the **current release (hookr-1: HookrRoot, HookrLauncher, HookrRouter, HookrTreasury, ...)** and an earlier one (MarketCoordinatorV5, ModularHookV6). The "open an existing-asset market" guide, the fee-collection guide and the config reference describe the **earlier** contracts. | Use the hookr-1 HookrLauncher. Do not use `coordinator.openExistingTokenMarket` (legacy). |
| Existing-token launch | hookr-1: "Any token that already exists can get new Hookr pools"; "the launcher only checks that the token is a contract"; the creator funds each pool from its own wallet with **opening price, price range, seed amounts, base fee (0.3% default)**, 1 to 8 pools; "The creator holds every pool's founding liquidity as one launch". | In hookr-1 the launch itself seeds the founding position. The legacy flow (empty pool, then PositionManager) is the brief's assumption; both paths are prepared (section 4). Whether hookr-1 accepts a **MILORD-only seed with 0 ETH** is not stated: EXTERNAL. |
| Who can open | Anyone, no token-owner check. | A third party could open a MILORD pool first with other settings. Our frontend and docs must link only the official pool id. |
| Fee-on-transfer | Rejected by the launcher. | MILORD has no transfer tax: compatible. |
| Launch fee | "0, can never go above 0.01 ETH", once per launch; read `launchFee()` on the launcher. | Budget ≤ 0.01 ETH. |
| Discovery | Not stated for existing-token launches; launch pages and Discover show lock status. | EXTERNAL: confirm on hookr.fun before launch. |

## 2. Tax + Conversion block

| Setting | Proposed | Docs limit / default | Status |
|---|---|---|---|
| Buy tax | **1.5%** | 0–10%, default 3% | NEEDS APPROVAL (approved by owner in the brief) |
| Sell tax | **1.5%** | 0–10%, default 3% | approved |
| Taxed side | quote (ETH) on buys and sells | "A tax on the quote side of buys and of sells" | matches |
| Conversion route | **none** (tax paid in ETH) | "Zero pays the tax in the quote" | matches |
| Buy/sell recipient | **TradingTaxSplitter** (both sides) | one recipient per side, frozen, must accept the quote | TESTED locally with a mock |
| Launch tax ("tax at launch") | **must not exceed 1.5%** | default 10%, **must be above the base tax**, decays over 1–100,000 blocks | **BLOCKER, EXTERNAL** (see below) |
| Decay | **0 (flat)** if accepted | page says "Zero keeps the tax flat" but also a 1-block minimum | EXTERNAL |
| Market-hours tiers | 0 | 0% default | off |
| Commission (Hookr share) | **20%** (the minimum) | creator picks 20–50% at launch, floor = max(20%, treasury floor), frozen | read live floor before launch |

**Launch-tax conflict.** The brief forbids any launch-period tax above 1.5%. The block's "tax at launch" must be
*above* the base tax. Only two outcomes are acceptable:
1. decay = 0 is accepted and keeps the tax flat at 1.5% from the first block (then "at launch" is irrelevant), or
2. Hookr confirms another way to start at exactly 1.5%.
If neither holds, the smallest deviation is a launch tax of 1.51% for 1 block (about 12 s), which still breaches the
approved ceiling and needs your explicit approval. **Not chosen; awaiting confirmation.**

## 3. Blocks to switch off (core defaults start ON)

| Block | Default | Setting | Why |
|---|---|---|---|
| Anti-Snipe | guard 50 blocks, launch tax up to 59.7% | **off** | would breach the 1.5% ceiling and block outside LPs during the guard |
| Dynamic fees | 2.3% ceiling on 0.3% base | **off** | would add fees above the disclosed rate |
| LP Rewards / Creator royalty | on | **off** | not in the agreed economics |
| Auto Burn | 1%, dead-address transfer on buys | **off** | brief: no additional Hookr Auto Burn |
| Arbitrage recapture | Hookr takes 25% of profit | **off** | not in the agreed economics |
| Tax + Conversion | — | **on** | it is the pool's single advisory slot |

## 4. Liquidity

| Item | Proposed | Status |
|---|---|---|
| Pool | one native ETH / MILORD pool on the HookrRoot hook | EXTERNAL |
| Base LP fee | **0.3%** (Hookr default), separate from tax, paid to the position | NEEDS APPROVAL |
| Tick spacing | 60 (Hookr default) | EXTERNAL |
| Position | MILORD only, opening price at the top of the range | TESTED (real v4) |
| Range, amount, price | **final:** ticks 134220–203280, all 6,767,000,000 MILORD, ~10.06 ETH FDV (LAUNCH_LIQUIDITY.md) | NEEDS APPROVAL |
| Ownership / lock | permanent HookrFamilyLock, beneficiary HookrLockFeeCollector → BuybackBurner (LP_LOCK.md) | APPROVED, NOT EXECUTED |

Two equivalent ways to create the founding position:

- **A. hookr-1 launcher seeds it** (preferred if it accepts MILORD-only): launch with price, range and a MILORD-only
  seed, then use Hookr's launch lock "forever" (beneficiary keeps fee collection).
- **B. empty market + PositionManager** (the brief's flow): after the market opens, the allocation Safe mints the
  MILORD-only position through the Uniswap v4 PositionManager (superseded: Hookr's launcher holds the family; see LP_LOCK.md) (no withdraw path,
  fees only to a fixed recipient). The Safe batch is generated by `offchain/safe-launch-batch.mjs` and was executed
  successfully against the local deployment. TESTED locally; BUILT, NOT INTEGRATION-TESTED against Hookr.

Sell-side reality (TESTED): with MILORD-only liquidity the pool holds **0 ETH** until someone buys. A sell before
any buy receives 0 ETH; afterwards sellers can only receive ETH that buyers put in (minus tax and LP fee).
With 100% of supply in the pool, no team or treasury MILORD exists to sell into it.

## 5. Fee collection

- Tax queues in the pool's quote. "Anyone can settle a queue, which splits off Hookr's share", then anyone pays
  out to the frozen recipient. If a transfer fails, the recipient receives PoolManager claims instead (ERC-6909),
  which **TradingTaxSplitter cannot redeem**: its `receive()` never reverts, so pushes should succeed. ETH vs WETH
  payout is not documented: the splitter handles both (`distribute(address(0))` and `distribute(WETH)`). EXTERNAL.
- **Revenue Router** (Hookr): up to 8 payees, 0.01% granularity, pull claims, documented for the creator royalty.
  Not chosen: it cannot do exact thirds, its use as a tax recipient is not confirmed, its address and ETH handling
  are undocumented, and it cannot pay the BuybackBurner as a burn budget with a record. **Recommendation: name
  the existing TradingTaxSplitter (33.34 / 33.33 / 33.33, bounded, payee changes delayed 2 days) as both
  recipients.** It cannot change the frozen pool tax.

## 6. Commission arithmetic

| Hookr share | Hookr per trade | Project per trade | Each of burn / team / arbitrage |
|---|---|---|---|
| 20% (proposed) | 0.30% | 1.20% | 0.40% |
| 25% | 0.375% | 1.125% | 0.375% |
| 50% (max) | 0.75% | 0.75% | 0.25% |

The splitter receives whatever the hook pays, so any commission is split equally with no contract change.
TESTED for 20–50% (fuzz).

## 7. Contracts (Robinhood Chain 4663, from Hookr docs, unverified on-chain)

| Contract | Address |
|---|---|
| HookrRoot (hook) | `0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc` |
| HookrLauncher | `0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4` |
| HookrRouter | `0xC2Bc9a3fe6c4Ce8C2F7EB4655b0869b01D8c93C9` |
| HookrQuoter | `0x445D7891381Aa7F91F5048EdBC6a611ccBDe7865` |
| HookrTreasury | `0x2242F7FC75606500503075f29ea8cDA684EfB4a9` |
| Uniswap v4 PoolManager | `0x8366a39CC670B4001A1121B8F6A443A643e40951` |
| PositionManager | `0x58daec3116aae6D93017bAAea7749052E8a04fA7` |
| Universal Router 2.1.1 | `0x8876789976dEcBfCbBbe364623C63652db8C0904` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |

## 8. Before generating production transactions

1. Pull the verified hookr-1 source/ABI for HookrLauncher and HookrRoot; confirm the existing-token launch
   signature, token-only seeding, decay = 0, the commission getter and the payout function.
2. Fork-test on a Robinhood Chain fork: launch → buy → sell → settle → payout to TradingTaxSplitter →
   `BuybackBurner` buyback through `V4SwapAdapter` on the real Hookr pool key (the hook may only accept swaps
   through HookrRouter; if so, the buyback route must call HookrRouter instead).
3. Confirm Discover visibility and the official pool id; publish it on the website.
