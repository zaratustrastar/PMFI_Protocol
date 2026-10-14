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
