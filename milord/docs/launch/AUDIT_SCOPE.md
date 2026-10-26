# Security-audit scope (draft, 2026-10-21)

Nothing in this repository is production-ready before an independent review. This page separates what is tested
code, what is an operational assumption, and what is not implemented.

## 1. In scope: new contracts (Solidity 0.8.26; production build via-IR, optimizer 200)
| Contract | Lines | Role | Tests |
|---|---|---|---|
| `src/launch/MilordAtomicLaunch.sol` | 273 | creates MILORD and launches it on Hookr in one Safe transaction; verifies every frozen term | fork (`MilordAtomicLaunchFork`, 9 tests, VPS) |
| `src/launch/HookrLockFeeCollector.sol` | 94 | beneficiary of the permanent lock: ETH fees → BuybackBurner, MILORD fees burned | unit + fork |
| `src/launch/V4SwapAdapter.sol` | 72 | BuybackBurner's exact-input buy on the Hookr pool | unit + fork |
| `src/robinhood/MILORDToken.sol` | 19 | fixed-supply ERC-20 with burn | unit |
| `src/robinhood/BuybackBurner.sol` | 160 | budgeted, operator-gated buyback-and-burn; timelocked adapter change | unit + fork |
| `src/robinhood/RevenueSplitter.sol` | 194 | bounded-share ETH/token splitters (entry fee, royalty, trading tax) | unit |
| `src/robinhood/MilordConverter.sol` | 333 | NFT entry/exit with signed-quote TWAP entry fee | unit + invariant + sim |
| `src/robinhood/MilordsNFT.sol` | 170 | 6,767 Milords, votes, royalty | unit |
| `src/robinhood/StrategyRegistry.sol` | 214 | characteristics and per-NFT strategy preferences | unit |
| `src/robinhood/MilordGovernor.sol` | 51 | stock OpenZeppelin Governor configuration | unit |
| `src/automation/ArbitrageFundingRouter.sol` | 209 | ETH → USDG → vault deposit, caps, slippage, pause | unit + sim |
| `src/automation/UniV3TwapReference.sol`, `ChainlinkReference.sol`, `DualReference.sol`, `UniV3SwapVenue.sol` | 229 | price references and venue | unit + sim |
| `src/parb/PMFIArbVaultV3.sol` | 683 | fresh pARB vault (diff against the legacy V2 is the review focus) | unit + `LegacyV2Bugs` |
| `src/parb/ParbFundingAdapter.sol`, `ShareDistributor.sol` | 286 | batched deposits, daily cumulative Merkle roots with veto delay; a root in review cannot be replaced | unit + invariant + sim |
| `script/MilordDeployer.sol`, `script/DeployRobinhood.s.sol` | | deployment wiring, ownership handover, testnet-only guard | unit |

Out of scope: `src/mocks/*`, `src/legacy/PMFIArbVaultV2.sol` (deployed, not modified; reviewed only as the base of the
V3 diff), `src/vendor/hookr/*` (interfaces copied from Hookr-1 at 7e771b3).

## 2. In scope: off-chain code that moves or prices value
| Code | Role |
|---|---|
| `offchain/services/lib/jobs.mjs`, `keeper.mjs` | keeper: tax-queue payout (fails closed), splitters, collector, bounded buyback, router, settle, root activation, lock watch |
| `offchain/services/epoch-publisher.mjs`, `root-checker.mjs`, `offchain/lib/eligibility.mjs` | daily pARB allocation and independent root check |
| `offchain/quote-server.mjs`, `offchain/lib/twap.mjs`, `entry-price.mjs`, `pool-price.mjs` | signed entry-fee quotes from a TWAP rebuilt from PoolManager events |
| `offchain/option-d-reconcile.mjs`, `offchain/lib/option-d.mjs` | Option D split from Swap and LPFeesCollected events (encodes only) |
| `offchain/safe-launch-batch.mjs` | Safe batch encoding (encodes only) |
| `bot/` funding path (USDG mode) | Arbitrage Funding Safe → Polymarket/Kalshi; execution logic unchanged |

## 3. External dependencies (trusted, not audited by us)
Hookr-1 on Robinhood Chain (HookrLauncher, HookrRoot, HookrRules, HookrDirectionalTax + tax queues, HookrFamilyLock,
treasury); Uniswap v4 PoolManager and v3 WETH/USDG pool; OpenZeppelin 5; Safe. Hookr-1 bytecode against source:
HOOKR_BYTECODE_VERIFICATION.md. Hookr owner powers that matter to MILORD: launch fee (capped by the helper), treasury
target (receives Hookr's 20%, not ours), admissions (checked at launch). The frozen pool terms cannot be changed.

## 4. Tested on a Robinhood mainnet fork (VPS, real Hookr-1)
Launch with full supply and 0 ETH, 10.06 ETH FDV, flat 1.5% / 20% commission, Anti-Snipe candidate maths, permanent
lock, fee collection, buyback through Hookr, Option D handover, pool squatting (confirmed, fixed by the atomic launch).
New in this round, compiled but not yet run on the VPS: the atomic launch suite and the no-ArbSys-stub pass.

## 5. Operational assumptions (not enforced by code)
- Safe signer sets and thresholds (B10); signers never sign `HookrLauncher.withdraw` for the MILORD family.
- The permanent lock is executed before `principalLockedUntil` (LOCK_PROCEDURE.md); until then principal trusts the Safe.
- Option D's 90/10 is a Safe calculation from public events, not enforced on-chain.
- Keeper, publisher, root checker and quote signer keys are held by the operator; the root checker runs on an
  independent RPC; the Safe vetoes bad roots within the delay.
- Robinhood `block.number` advances about every 12 s (probe: CLOCK_AND_ARBSYS.md).
- The off-chain reporter signs vault NAV honestly (the vault bounds it; it cannot prove it).
- Polymarket/Kalshi funding routes for USDG (B9).

## 6. Not implemented
- Launch-and-lock in one transaction (deliberately; the lock needs its own approval).
- An on-chain 90/10 Option D collector (Option A1 in the Anti-Snipe report; Option D needs none).
- A Hookr conversion route for the tax (direct ETH payout is used; the keeper refuses a queue with a route).
- Monitoring dashboards and paging beyond the services' webhook alerts and Prometheus metrics.
- Art, metadata, website (B11).

## 7. Suggested focus
1. `MilordAtomicLaunch`: completeness of `_verify`, the CREATE2 / dust / `transferFamily` path, reentrancy through
   Hookr callbacks during `launchFamily`.
2. Vault V3 diff and the funding adapter / distributor accounting.
3. Converter entry pricing (signed quotes, TWAP floor) and exit burn.
4. Router bounds and the price references.
5. Keeper fail-closed logic and the Option D reconciliation arithmetic.
