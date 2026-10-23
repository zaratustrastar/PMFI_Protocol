# Arbitrage funding readiness (the key blocker)

> **2026-10-14 (Bobo):** keep the existing Polymarket and Kalshi execution infrastructure; change only Robinhood USDG
> funding, withdrawal and NAV reconciliation. **Opinion is disabled for V1** (insufficient liquidity); its adapter stays
> in the code for later. The Opinion column below is kept for reference only. The on-chain side (Safe → USDG →
> vault → pARB → claims) is covered in [PARB_AUTOMATION.md](PARB_AUTOMATION.md).

The V3 vault, pARB and the distributor are on Robinhood Chain (4663) with USDG. The funding bot
(`bot/arb_monitor/core/`) is written for the Base V2 vault with USDC: it moves vault cash to a servicer wallet on
Base and funds Polymarket and Kalshi from Base, and Opinion through a Base→BSC bridge. **No real funds were
moved; no venue or bridge API was called with credentials.** Route facts come from public docs and public
LI.FI/Across/Relay quotes on 2026-10-13 (EXTERNAL).

## 1. USDG on Robinhood Chain

| Check | Result | Status |
|---|---|---|
| Address | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` (Paxos docs) | EXTERNAL, confirm on explorer |
| Decimals | 6 (LI.FI token list) | EXTERNAL; vault/adapter/sim assume 6 and check it at deploy |
| Upgradeable / controls | UUPS proxy; PAUSE_ROLE; ASSET_PROTECTION_ROLE can freeze and wipe | EXTERNAL; risk register R-07 |
| Liquidity | Uniswap V3 WETH/USDG `0x69bf…8d9a`, ~$5.07M reserves, ~$3.4M daily volume | EXTERNAL |
| Do not use | `0x0A3B…954F` "Paxos USDG" (origin unknown) | — |
| USDC / CCTP on 4663 | none | — |

## 2. The eight questions per venue

| | Polymarket | Kalshi | Opinion |
|---|---|---|---|
| Deposit route | **USDG on 4663 directly** to the Polymarket bridge deposit address (listed on bridge.polymarket.com/supported-assets) | USDG 4663 → USDC on Base via Across or Relay, then the existing Kalshi Base deposit | USDG 4663 → BSC via LI.FI (or Relay), then the existing Opinion deposit |
| Withdrawal route | pUSD → bridge out; **arrival as USDG on 4663 unconfirmed**; fallback Polygon USDC → Across/Relay → USDG | USDC on Base → Across/Relay → USDG 4663 | BSC → LI.FI/Relay → USDG 4663 |
| Accepted coins | USDG, ETH, WETH, USDe on 4663 (min $2) | USDC (Base, Solana, Ethereum) | BSC stablecoin. **Docs say USDT; the bot uses BSC USDC `0x8AC7…580d`. Confirm.** |
| Costs | bridge cost only (Polymarket fee 0) | Across ~$0.10, Relay ~$0.32 per $1k; LI.FI default adds 0.25% | LI.FI 0.25% + bridge; taker fee 0–1% (min $0.25) |
| Timing | deposit minutes; withdrawal "instant" + bridge | Across/Relay ~2 s + Kalshi ~30 min | ~4–8 s bridge + venue |
| Minimums | $2 deposit | unpublished (old FAQ $10) | $5 withdrawal |
| Restrictions | geoblocked in US, UK, FR, DE, IT, PL, SG, TH, TW, AU, BE, BR, RU, some CA provinces | full KYC; **crypto withdrawals capped at $2,500/day** and need a prior crypto deposit | restricted in US, UK, FR, Ontario, SG, PL, TH, KR |
| NAV while in transit | must count (see §4) | must count | must count |

Paxos mint/redeem (1:1) and Paxos Labs Transit are institutional alternatives for large amounts (EXTERNAL).

## 3. Smallest safe change to the bot

Keep the strategy, venue clients and ledger. Change only where money enters or leaves the Base wallet.

| File | Where | Change |
|---|---|---|
| `arb_funder.py` | `:47-52` constants, `:97-123` balance/allowance helpers | make chain, RPC and token configurable; servicer cash = USDG on 4663 (6 decimals) |
| `arb_funder.py` | `:389` `_call_tend_if_ready` | point at the V3 vault on 4663 (same `tend()`; `arb_reporter.py:42-44` already has the V3 env switch) |
| `arb_funder.py` | `:589` `ensure_funded_for_trade`, `:705` `fund_both_legs_for_trade` | Polymarket: send USDG on 4663 to the Polymarket deposit address. Kalshi: new `_bridge_usdg_to_base_usdc()` via Across (Relay fallback), then the existing Kalshi transfer. Opinion: replace `_bridge_usdc_base_to_bsc` (`:272`) source chain/token with 4663 USDG |
| `arb_withdrawals.py` | `:21-27` constants, `:252` `_relay_bridge_polygon_to_base`, `:538` `_bridge_bsc_to_base` | destination = USDG on 4663 instead of Base USDC |
| `arb_withdrawals.py` | `:108` `mark_withdrawals_arrived` | compare against servicer USDG on 4663 |
| `arb_nav.py` | `:183` `_get_servicer_wallet_usdc_on_base` | read USDG on 4663; add USDC still on Base (Kalshi leg) and pending bridge amounts |
| `arb_nav.py` | `:515` `compute_nav` | add `get_pending_withdrawals_total_usdc()` and outbound bridge in-flight amounts (currently **not counted**, so NAV dips while money is in a bridge); apply a USDG/USDC peg haircut if the peg is off by more than a threshold |
| config | env | `ARB_VAULT_CHAIN_ID=4663`, RPC, USDG address, deposit addresses; secrets untouched |

Code note: `compute_nav`'s docstring says open positions are valued at $0.99 per pair, but the code sums
`cost_basis_usdc` (`arb_nav.py:543`). Not changed; confirm which is intended.

Use Across/Relay directly rather than LI.FI's default route to avoid the 0.25% fee where possible.

**Implemented (offline-tested, not run against live venues or chains).** `ARB_FUNDING_CHAIN=robinhood`
(default `base` keeps the V2 path unchanged, Opinion included) switches servicer cash to USDG on 4663,
`tend()` to the V3 vault on 4663, Polymarket funding to a USDG transfer to `POLY_RH_DEPOSIT_ADDR`, and
Kalshi funding to an Across `exactOutput` quote (Relay fallback, fee cap `ARB_BRIDGE_MAX_FEE_BPS`=50,
approve-only pre-txs, no native value) followed by the existing Base USDC transfer. Withdrawals land as
USDG on 4663 (Polymarket script destination set by env; Kalshi USDC on Base is swept back each tend tick).
Bridge legs go to an in-transit ledger; `compute_nav` adds pending withdrawals and in-transit bridges,
with an optional peg haircut (`ARB_PEG_MAX_DEVIATION_BPS`). In robinhood mode Opinion is off unless `ARB_OPINION_ENABLED=true`; base mode keeps it on.
Tests: `python3 -m unittest discover -s bot/arb_monitor/tests -v`. The Opinion row (`:272`) was not
changed because Opinion is disabled for V1.

## 4. NAV rule while funds move

NAV = vault USDG + servicer USDG (4663) + servicer USDC (Base) + venue cash (Polymarket, Kalshi, Opinion) + open
positions + **in-transit (bridge sent, not yet arrived)**. Without in-transit, every bridge leg looks like a loss
and every arrival like a profit, which would move PPS on capital movements. The vault's settle step should run
only when nothing is in transit or the in-transit amount is counted.

## 5. What is needed to finish

| Item | Owner | Status |
|---|---|---|
| Confirm Polymarket withdrawal can land as USDG on 4663 (one small test) | team, real funds, approval needed | EXTERNAL |
| Confirm Kalshi entity account and the $2,500/day crypto withdrawal cap for our volumes | team | EXTERNAL |
| Confirm Opinion collateral token (USDT vs USDC) | team | EXTERNAL |
| Implement the table in §3 | Claude | DONE, offline tests only (mocked RPC/HTTP) |
| Confirm Across and Relay support Robinhood Chain 4663 ⇄ Base, and the exact quote/status field names | team | EXTERNAL |
| Test against small amounts | team, real funds, approval needed | NOT STARTED |
| Decide whether Kalshi stays given the cap | owner | NEEDS APPROVAL |
