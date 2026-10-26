# Open decisions

Items that need an owner decision or an input that cannot be substituted. Defaults used in the local build
are marked; none of them contradicts the spec.

## Blocking a production launch

| # | Decision | Options | Local default / recommendation |
|---|---|---|---|
| D1 | Trading-tax mechanism | **Resolved 2026-10-13: Hookr existing-token pool, Tax + Conversion 1.5%/1.5% in ETH, immutable, no custom hook** | See launch/HOOKR_FINAL_CONFIG.md |
| D10 | Launch values | **Final (Bobo 2026-10-14): 100% of supply in the founding position, 0 outside, 0 ETH, ~10 ETH FDV, range ~1,000×; no migration reserve** | launch/LAUNCH_LIQUIDITY.md |
| D14 | LP lock type, position owner, fee beneficiary | APPROVED 2026-10-14: permanent HookrFamilyLock, beneficiary HookrLockFeeCollector (ETH → BuybackBurner, MILORD burned). Execution waits for a separate go-ahead | launch/LP_LOCK.md |
| D15 | Funding automation | **Authorized 2026-10-14**: ArbitrageFundingRouter is the arbitrage payee of all three splitters (ETH → USDG → vault → pARB → daily cumulative Merkle claims), with caps, slippage bound, independent TWAP reference, Safe pause, no keeper withdrawals | launch/PARB_AUTOMATION.md |
| D11 | Hookr launch-tax decay (must not exceed 1.5%) | **Resolved**: flat launch, Anti-Snipe off; fork-verified flat 1.5% from the first trade with no surcharge. A 40% → 0 over 60 min Anti-Snipe stays an unapproved candidate | launch/HOOKR_FINAL_CONFIG.md |
| D12 | Hookr commission, LP fee, lock type, LP fee recipient | **Approved 2026-10-14**: 20% (fork-verified) · 0.3% · forever · HookrLockFeeCollector (ETH → buyback-and-burn, MILORD burned) | launch/HOOKR_FINAL_CONFIG.md |
| D13 | Tax recipient | **TradingTaxSplitter** · Hookr Revenue Router | splitter (exact thirds, burn record) |
| D2 | Milords artwork + metadata | must be supplied | `ipfs://PENDING-METADATA/` placeholder; `freezeMetadata()` after upload |
| D3 | 8 characteristic names (and any mapping to art traits) | must be supplied | "Characteristic 1–8" placeholders |
| D4 | pARB instance | **new V3 instance on Robinhood Chain, asset USDG, 0% fee** · reuse V2 on Base (20% fee, PPS bugs) | V3 on Robinhood (owner instruction 2026-10-13) |
| D5 | USDG address and decimals on Robinhood Chain; engine funding route Robinhood USDG ↔ Polymarket/Kalshi | USDG `0x5fc5…1d168` (6 decimals) matches Paxos's docs; routes documented in launch/B9_VENUE_ROUTES.md (Polymarket bridge both ways; Kalshi via Across + manual withdrawals) | first live small transfers need owner approval |
| D6 | Robinhood Chain mainnet RPC | public `https://rpc.mainnet.chain.robinhood.com` used for forks; a second, independent provider is needed for the root checker and monitor | — |
| D7 | Initial MILORD allocation | **resolved: 100% to the founding position (D10)** | legacy PMFI handled by a disclosed off-chain process, no contract |
| D9 | Kalshi withdrawals: which chain and asset Kalshi can pay out to | USDC; crypto withdrawals are made in the Kalshi app (no API creates one) and need a prior crypto deposit; limits shown in the app | robinhood-mode recall is an operator task (launch/B9_VENUE_ROUTES.md); **Kalshi kept, Opinion disabled for V1 (Bobo 2026-10-14)**; MILORD's own Kalshi account is an owner action |
| D8 | Website source project | not in repo; only compiled artifacts | integration delivered as a drop-in SDK + section markup |

## Parameters to approve

| # | Parameter | Local value | Note |
|---|---|---|---|
| P1 | Converter fees | **current: entry max(10% × ETH value of 1M MILORD, 0.002 ETH), exit burn 10%** (approved); hard ceilings 20%, 0.02 ETH, 10% | ceilings immutable; increases wait 2 days, reductions are immediate |
| P2 | Splitter bounds | [0, 10000] locally | proposed: entry arbitrage [6000, 8000]; royalty [4000, 6000]; tax [2500, 4000] each |
| P3 | Quote lifetime | 5 min (cap 30) | |
| P4 | Max PPS move per report | 10% | |
| P5 | Root activation delay | 1 day (local 1 h) | |
| P6 | Royalty ceiling | 10% | |
| P7 | Governor | advisory only in V1 | quorum 10%, 1-day delay, 5-day period |
| P8 | Buyback size limits | 0.01 – 5 ETH per call | |
| P9 | Eligibility epoch | UTC day, 00:00–00:00 (Bobo 2026-10-26: daily, was weekly) | |

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
