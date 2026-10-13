# Open decisions

Items that need an owner decision or an input that cannot be substituted. Defaults used in the local build
are marked; none of them contradicts the spec.

## Blocking a production launch

| # | Decision | Options | Local default / recommendation |
|---|---|---|---|
| D1 | Trading-tax mechanism (Hookr conflict) | A static 1.5% v4 pool · **B minimal dynamic-fee hook** · C custom tax hook · D Hookr frozen params | **B** recommended; nothing implemented yet. See HOOKR_COMPATIBILITY. |
| D2 | Milords artwork + metadata | must be supplied | `ipfs://PENDING-METADATA/` placeholder; `freezeMetadata()` after upload |
| D3 | 8 characteristic names (and any mapping to art traits) | must be supplied | "Characteristic 1–8" placeholders |
| D4 | pARB instance | **new V3 instance on Robinhood Chain, asset USDG, 0% fee** · reuse V2 on Base (20% fee, PPS bugs) | V3 on Robinhood (owner instruction 2026-10-13) |
| D5 | USDG address and decimals on Robinhood Chain; engine funding route Robinhood USDG ↔ Polymarket/Kalshi/Opinion | to confirm on-chain; route to build | address seen on aggregators `0x5fc5…1d168` (unverified); vault enforces 6 decimals; fees no longer bridge |
| D6 | Robinhood Chain mainnet RPC | to supply | — |
| D7 | Initial MILORD allocation (liquidity, team, legacy holders, treasury) | not in brief | all to deployer in local build |
| D9 | Kalshi withdrawals: which chain and asset Kalshi can pay out to | to confirm with Kalshi | until known, Kalshi recalls land on Base USDC and are bridged |
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
