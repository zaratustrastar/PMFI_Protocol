# Open decisions

Items that need an owner decision or an input that cannot be substituted. Defaults used in the local build
are marked; none of them contradicts the spec.

## Blocking a production launch

| # | Decision | Options | Local default / recommendation |
|---|---|---|---|
| D1 | Trading-tax mechanism | **Resolved 2026-10-13: Hookr existing-token pool, Tax + Conversion 1.5%/1.5% in ETH, immutable, no custom hook** | See launch/HOOKR_FINAL_CONFIG.md |
| D10 | Launch values | **Final (Bobo 2026-10-14): 100% of supply in the founding position, 0 outside, 0 ETH, ~10 ETH FDV, range ~1,000×; no migration reserve** | launch/LAUNCH_LIQUIDITY.md |
| D14 | LP lock type, position owner, fee beneficiary | APPROVED 2026-10-14: permanent HookrFamilyLock, beneficiary HookrLockFeeCollector (ETH → BuybackBurner, MILORD burned). Execution waits for a separate go-ahead | launch/LP_LOCK.md |
| D15 | Weekly funding automation | ArbitrageFundingRouter as arbitrage payee proposed | launch/PARB_AUTOMATION.md |
| D11 | Hookr launch-tax decay (must not exceed 1.5%) | decay 0 if it keeps the tax flat; else owner decision | to confirm on the hookr-1 contract |
| D12 | Hookr commission, LP fee, lock type, LP fee recipient | 20% · 0.3% · forever · Team Safe proposed | launch/HOOKR_FINAL_CONFIG.md |
| D13 | Tax recipient | **TradingTaxSplitter** · Hookr Revenue Router | splitter (exact thirds, burn record) |
| D2 | Milords artwork + metadata | must be supplied | `ipfs://PENDING-METADATA/` placeholder; `freezeMetadata()` after upload |
| D3 | 8 characteristic names (and any mapping to art traits) | must be supplied | "Characteristic 1–8" placeholders |
| D4 | pARB instance | **new V3 instance on Robinhood Chain, asset USDG, 0% fee** · reuse V2 on Base (20% fee, PPS bugs) | V3 on Robinhood (owner instruction 2026-10-13) |
| D5 | USDG address and decimals on Robinhood Chain; engine funding route Robinhood USDG ↔ Polymarket/Kalshi/Opinion | to confirm on-chain; route to build | address seen on aggregators `0x5fc5…1d168` (unverified); vault enforces 6 decimals; fees no longer bridge |
| D6 | Robinhood Chain mainnet RPC | to supply | — |
| D7 | Initial MILORD allocation | **resolved: 100% to the founding position (D10)** | legacy PMFI handled by a disclosed off-chain process, no contract |
| D9 | Kalshi withdrawals: which chain and asset Kalshi can pay out to | to confirm with Kalshi | USDC on Base/Solana/Ethereum only, crypto withdrawals capped at $2,500/day; **Kalshi kept, Opinion disabled for V1 (Bobo 2026-10-14)** |
| D8 | Website source project | not in repo; only compiled artifacts | integration delivered as a drop-in SDK + section markup |

## Parameters to approve

| # | Parameter | Local value | Note |
|---|---|---|---|
| P1 | Converter ceilings | entry 20%, min fee 0.02 ETH, exit burn 10% | immutable once deployed |
| P2 | Splitter bounds | [0, 10000] locally | proposed: entry arbitrage [6000, 8000]; royalty [4000, 6000]; tax [2500, 4000] each |
| P3 | Quote lifetime | 5 min (cap 30) | |
| P4 | Max PPS move per report | 10% | |
| P5 | Root activation delay | 1 day (local 1 h) | |
| P6 | Royalty ceiling | 10% | |
| P7 | Governor | advisory only in V1 | quorum 10%, 1-day delay, 5-day period |
| P8 | Buyback size limits | 0.01 – 5 ETH per call | |
| P9 | Eligibility week | UTC Monday 00:00 | |

## Legacy PMFI holders (documented only, no contract)

Owner's intended process: after an announcement, willing PMFI holders send Base PMFI to an announced wallet;
the owner sells the legacy tokens, buys MILORD and sends each holder their proportional amount. Nothing in this
repository automates it. Recommendations if it goes ahead:

- Publish eligibility rules, the receiving address, cut-off time and how the rate is set **before** accepting
  tokens. Do not state a guaranteed ratio: execution prices and slippage decide it.
- Keep a ledger per holder: Base tx hash in, PMFI amount, Robinhood Chain address (signed message from the sending
  Base address), sale tx(s), MILORD purchase tx(s), MILORD sent tx. Publish an aggregate reconciliation.
- Reserve MILORD for it outside the launch pool (D7), held in a Safe, not a personal wallet.
- Contract holders (LP positions, Safes) need a separate address-verification path.
- Get legal/tax advice on custody and on selling holders' tokens on their behalf.

## Website copy to change

See CURRENT_PROJECT_AUDIT §5: entry/exit amounts, "20% of profit", migration "through a contract",
and the wallet sentence.
