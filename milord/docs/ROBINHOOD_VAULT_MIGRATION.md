# pARB vault on Robinhood Chain with USDG

Owner instruction (2026-10-13): deploy a fresh V3 vault, pARB and the distributor (daily epochs since 2026-10-26) on Robinhood Chain
instead of Base, with USDG as the candidate asset, keeping the V3 implementation unchanged where possible.
Nothing has been deployed to any public network.

## Answer

For the contracts, the move is configuration plus one guard. The share accounting, `report`/`tend`
mechanics, the V3 fixes, `ParbFundingAdapter` and `ShareDistributor` are unchanged. What did change:

| Change | Where | Why |
|---|---|---|
| Constructor requires `decimals() == 6` (V3-10) | `src/parb/PMFIArbVaultV3.sol` | `INITIAL_PPS`, `MIN_DEPOSIT_USDC`, `minimumIdleAmount` and the reporter's 1e6 scaling all assume 6 decimals. Fails closed if USDG on Robinhood is not 6. |
| `BasePayoutRegistry` removed | `src/robinhood` | rewards are on the same chain, so pARB is paid to the holder address; no reward bridge |
| One-chain deployer | `script/MilordDeployer.sol`, `DeployLocal.s.sol`, `DeployRobinhood.s.sol` | deploys token, NFTs, splitters, vault, adapter and distributor together; registry strategy uses `block.chainid` |
| Testnet-only script | `script/DeployRobinhood.s.sol` | reads `MILORD_*` env addresses (no keys); reverts on 4663 ("mainnet deployment not approved") and on any chain but 46630 |
| Reporter chain settings via env | `bot/arb_monitor/core/arb_reporter.py:42-49` | `ARB_VAULT_DOMAIN_SALT`, `ARB_VAULT_CHAIN_ID`, `ARB_VAULT_RPC_URL`, `ARB_VAULT_ASSET`; defaults keep the live Base V2 behaviour |
| Folder rename `src/base` → `src/parb` | | names only; variable names keep `usdc` so ABIs are identical |

The vault has no Base-specific code: its EIP-712-style report hash already binds `block.chainid` and the vault
address, and the asset is an immutable constructor argument.

## 1. USDG checks

| Item | Status |
|---|---|
| Issuer | Paxos (Global Dollar). Natively minted on Robinhood Chain. |
| Address | `0x5fc5360d0400a0fd4f2af552add042d716f1d168` as listed by DEX aggregators. **Not verified on-chain from here** (explorer blocked); confirm against Paxos's published list before use. |
| Decimals | Paxos tokens use 6 on Ethereum; **not verified on Robinhood**. The vault constructor and the deployer both revert unless 6, so a wrong assumption cannot deploy. |
| Liquidity | about $8.5M in Uniswap pools and about 65% of the chain's stablecoin supply (public reports, July 2026). Enough for routine fee swaps of the expected size; not verified for engine-sized transfers. |
| Compatibility | Standard ERC-20, no fee-on-transfer, no rebasing: compatible with `SafeERC20` and the vault's balance checks. |
| Issuer controls | Upgradeable proxy (admin can change code), `PAUSE_ROLE` pauses all transfers, `ASSET_PROTECTION_ROLE` can freeze an address and wipe its balance. A freeze of the vault would block deposits, `tend` and redemptions; it cannot touch pARB already claimed. |

Tests with `MockUSDG` (same pause and freeze behaviour) show: while USDG is paused, holders can still claim
pARB; `fund` and `claimRedeem` revert and resume after unpause. If the vault is frozen, pARB still transfers
but cannot be redeemed until unfrozen.

## 2. Fee-to-pARB flow (now one chain)

Splitters → Arbitrage Safe ETH → Safe swap ETH→USDG on Robinhood → `adapter.fund(sourceRef, amount)` →
vault report at official PPS → `adapter.settle` → shares to `ShareDistributor` → daily cumulative root →
holder claims → holder may `requestRedeem` for USDG. `sourceRef` is single-use, so the same fees cannot be
turned into shares twice. Covered end to end in `test/robinhood/FeeToParbFlow.t.sol` and the 3-week simulation.

## 3. Code that genuinely needs changing (arbitrage engine funding)

The reward bridge is gone, but the engine trades on Polymarket (Polygon), Opinion (BSC) and Kalshi. Its
capital currently leaves and returns through Base USDC. These paths must gain a Robinhood USDG leg before the
new vault can fund the engine. **Update 2026-10-23:** the robinhood funding mode exists (`ARB_FUNDING_CHAIN=robinhood`):
USDG on 4663 ↔ Base USDC ↔ venues, with allowlists, receipts, write-ahead intents and delta-based arrival
(READINESS_MATRIX.md rows 3–4). The table below is the original gap list; what remains open is route verification
(B9), CTF redemption (B21) and the TS full-balance bridge (B22). Opinion stays disabled.

| File:line | Today | Needs |
|---|---|---|
| `bot/arb_monitor/core/arb_funder.py:47-51` | Base RPC, chain 8453, `USDC_BASE` | Robinhood RPC and USDG for the servicer's balance and approvals |
| `arb_funder.py:269-365` (`_bridge_usdc_base_to_bsc`) | LI.FI Base USDC → BSC for Opinion | LI.FI Robinhood USDG → BSC USDT (LI.FI supports Robinhood Chain; route and depth to confirm) |
| `arb_funder.py:616`, `bot/arb_monitor/config.py:210-212` | `POLY_/KALSHI_/OPINION_BASE_DEPOSIT_ADDR`: venue deposit addresses that accept **Base USDC** | either a hop Robinhood USDG → Base USDC before the existing code, or venue deposit addresses on a chain they support |
| `bot/arb_monitor/core/arb_withdrawals.py:21-25` | Base chain id and USDC | Robinhood chain id and USDG |
| `arb_withdrawals.py:252-326` (`_relay_bridge_polygon_to_base`) | Relay Polygon USDC.e → Base USDC | destination Robinhood USDG (Relay support for Robinhood to confirm) |
| `arb_withdrawals.py:462-505` (Kalshi withdrawal) | Kalshi pays USDC to `dest_address` | Kalshi will not pay USDG on Robinhood as far as we know: land on a supported chain, then bridge (D9) |
| `arb_withdrawals.py:538-626` (`_bridge_bsc_to_base`) | LI.FI BSC → Base | LI.FI BSC → Robinhood USDG |
| `bot/arb_monitor/core/arb_nav.py:183-199` | servicer cash read on Base USDC | read servicer USDG on Robinhood, plus any in-flight bridge balance |

Smallest viable option: keep the existing Base-side funder untouched, give the servicer one extra step
(Robinhood USDG → Base USDC out, Base USDC → Robinhood USDG back via LI.FI), and count Base and in-flight
balances in NAV. The alternative (rewriting every venue route to Robinhood) touches more code for no
accounting benefit.

**Peg assumption.** Venue balances are USDC, USDC.e or USDT, but the vault's NAV is in USDG units. The
reporter adds them 1:1. A depeg or swap slippage shows up as PPS change; the reporter should value non-USDG
balances at market and log the rate.

## 4. Configuration

| Setting | Robinhood value |
|---|---|
| Chain id | 4663 mainnet, 46630 testnet |
| Vault asset | USDG (6 decimals, address to confirm) |
| Reporter | `ARB_VAULT_DOMAIN_SALT=PMFIArbVaultV3.milord.v1`, `ARB_VAULT_CHAIN_ID=4663`, `ARB_VAULT_RPC_URL`, `ARB_VAULT_ASSET` (= `ARB_USDG_ADDRESS`), `ARB_VAULT_V2_ADDRESS=<V3 vault>`; refuses to start on missing values or Base settings; `ARB_REPORT_GAS_LIMIT`, `ARB_RECALL_COOLDOWN_SECS` (1800) |
| Funding (required) | `ARB_FUNDING_CHAIN=robinhood`, `ROBINHOOD_CHAIN_ID`, `ARB_USDG_ADDRESS`, `ARB_BRIDGE_ALLOWLIST` (`chainId:0xaddr,…`, empty refuses every bridge tx), `POLY_RH_DEPOSIT_ADDR/_CHAIN_ID/_TOKEN`, `KALSHI_BASE_DEPOSIT_ADDR/_CHAIN_ID/_TOKEN` |
| Funding (tunable) | `ARB_TX_RECEIPT_TIMEOUT_SECS` (90), `ARB_HTTP_TIMEOUT_SECS` (15), `ARB_HTTP_RETRIES` (2), `ARB_ARRIVAL_TOLERANCE_BPS`, `ARB_INFLIGHT_STALE_SECS` (86400), `ARB_ALLOWED_TOKENS_EXTRA`, ledger paths `ARB_INTENT_LEDGER_PATH`, `ARB_CASHFLOW_JOURNAL_PATH`, `ARB_VENUE_DEPOSIT_LEDGER_PATH` |
| Bridge status for the monitor | `ARB_BRIDGE_STATUS_HOST` (127.0.0.1), `ARB_BRIDGE_STATUS_PORT` (9110) |
| Registry strategy | `pArbitrage` at (4663, vault, vault) |
| Testnet deploy env | see the header of `script/DeployRobinhood.s.sol` (adds `MILORD_PUBLISHER`, `MILORD_FUNDING_POOL`, `MILORD_WETH`, `MILORD_ROUTER_PAUSER`, `MILORD_PREDICTED_TOKEN` and the router bounds) |

## 5. Tests added

| File | Covers |
|---|---|
| `test/robinhood/RobinhoodDeployment.t.sol` (8) | wiring to USDG; registry on local chain; Safe ownership; production splitter bounds; 18-decimal asset rejected; Base-signed report rejected on Robinhood; script refuses 4663 and 8453; testnet deploy from env |
| `test/robinhood/FeeToParbFlow.t.sol` (6) | week-1 time-weighted allocation; two weeks with a loss (PPS < 1), reissued NFT, cumulative claims, pARB redeemed for USDG; duplicate `sourceRef` rejected; no fee on profit; USDG pause; USDG freeze |
| `test/robinhood/ReporterCompat.t.sol` (1) | a report signed by the real `arb_reporter.py` with Robinhood env settings is accepted by V3 |
