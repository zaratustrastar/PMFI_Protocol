# Status update (2026-10-21): Robinhood fork suite

The VPS can now fork Robinhood Chain, and `test/fork/` tests B1–B4 against the deployed Hookr-1 contracts
(docs/launch/FORK_INTEGRATION.md, one command: `./fork/run-fork-tests.sh`). Until that run reports green:

| # | Status |
|---|---|
| B1 | Resolved from source, fork run confirms: Tax + Conversion is 0xE73f…1e1F / 0x9901…5d3c; config is `abi.encode(Config)` (320 bytes), flat 1.5% from the launch block with no launch-tax decay |
| B2 | Resolved from source, fork run confirms: commission = `protocolShareBps` 2000, applied as 20% of the tax (queue split) and 20% of any Snipe; no Hookr minimum fee asserted |
| B3 | Fork run decides: `test_buybackAndBurnThroughHookr` buys through V4SwapAdapter (unauthenticated) |
| B4 | Resolved from source, fork run confirms: `lock` performs `acceptFamily`; forever = 2^64-1; `collect` pays the beneficiary directly; `claims = 0` pays tokens |
| B5 | Resolved by the VPS (forks 4663 locally); this environment still cannot |
| New B13 | High: an outsider may open the ETH/MILORD Hookr pool first (ETH-only, no MILORD), making our launch revert. Fix: deploy MILORD and launch atomically. `test_poolSquattingRisk` confirms or rules it out |
| New B14 | High: the tax is pulled, not pushed. The keeper must call `payCreator()` on both tax queues before `splitter.distribute` |
| New B15 | Medium: `offchain/safe-launch-batch.mjs` must encode `launchFamily` with the fork-tested parameters |

# Remaining blockers, most severe first (2026-10-14)

| # | Severity | Blocker | Who can clear it | What clears it |
|---|---|---|---|---|
| B1 | Critical (launch) | Hookr Tax + Conversion advisory address and config encoding are not published; the launch call cannot be encoded, and the flat 1.5% from trade one with no snipe/guard surcharge cannot be proven | Hookr / Bobo | Hookr publishes the advisory address + config schema, then a Robinhood fork run (HOOKR_VERIFICATION.md) |
| B2 | Critical (launch) | Hookr commission of 20% not confirmed on the deployed contracts (field unclear: `protocolShareBps` vs advisory `quoteTakePips`) | Hookr / fork test | fork run measuring the split of the first trades |
| B3 | High | HookrRoot may refuse or treat differently unauthenticated swaps; BuybackBurner's V4SwapAdapter swaps directly on PoolManager | fork test | if refused: a HookrFamilyRouter adapter (BuybackBurner already supports a timelocked adapter change) |
| B4 | High | Lock details: exact "forever" `unlockBlock`, `transferFamily` `claims` argument, whether `lock` performs `acceptFamily`, whether `collect` pays `msg.sender` or holds ERC-6909 claims | fork test | fork run steps 4-5; HookrLockFeeCollector already handles both payout forms |
| B5 | High | No fork testing is possible from this environment: RPC, explorer and Sourcify API hosts are blocked by the network proxy | workspace admin | allow `rpc.mainnet.chain.robinhood.com`, `robin.etherscan.io`, `sourcify.dev` in a custom cloud environment (Project settings → environment) or run the fork script locally |
| B6 | High (pre-mainnet) | Independent security review of all new contracts (router, references, venue, collector, converter, splitters, distributor, vault V3 diff) | Bobo | auditor engagement; nothing is production-ready before it |
| B7 | Medium | WETH/USDG Uniswap V3 pool on Robinhood Chain: address, depth and observation cardinality not verified; the router's TWAP needs >= 30 min of history and enough cardinality | fork / on-chain read | read the pool; call `increaseObservationCardinalityNext` if low (anyone can) |
| B8 | Medium | No Chainlink ETH/USD feed confirmed on 4663: the router runs on the V3 TWAP alone unless a second reference is added (DualReference is ready) | Bobo | choose: TWAP only, or TWAP + feed |
| B9 | Medium | Funding bot (Polymarket/Kalshi): Across/Relay support for 4663 and a Polymarket USDG deposit route are unconfirmed; bridge deposit contracts are not allowlisted | Bobo / engine team | confirm routes; add a bridge-contract allowlist before live funds |
| B10 | Medium | Safe signer sets and thresholds for Team Safe and Arbitrage Funding Safe; router production bounds (SAFE_CONFIGURATION.md §3) | Bobo | confirm values |
| B11 | Low | Art, metadata, characteristic names, website hosting | Bobo | content |
| B12 | Low | Robinhood Chain `finalized` block tag support for the epoch publisher (falls back to 64 confirmations) | fork / docs | check RPC |

Not blockers (done and tested locally): permanent-lock fee path, LP fee burn, automation router with caps / TWAP / pause / sweep-to-owner, keeper / epoch publisher / root checker services with failure tests, funding bot USDG mode, Safe batches (lock batch prepared, not executed).
