# Hookr-1 compatibility verification (2026-10-14)

> 2026-10-21: superseded for B1–B4 by the Robinhood fork suite and the Hookr-1 source review in FORK_INTEGRATION.md.

**Result: not yet verifiable on chain. No incompatibility with the approved tokenomics was found in the published ABIs, but four parameters cannot be confirmed without a Robinhood Chain fork, and the launch call cannot be encoded until Hookr publishes the Tax + Conversion advisory address and config format.** Tokenomics were not changed to work around anything.

## What was checked and how
- Verified ABIs of the hookr-1 contracts were read from Sourcify (`sourcify.dev`, chain 4663) together with hookr.fun/docs.
- Calls to `rpc.mainnet.chain.robinhood.com`, `robin.etherscan.io` and direct `sourcify.dev` API access are refused by this environment's network proxy, so no fork test, no `eth_call` and no bytecode comparison was possible. An admin can allow those hosts under [Project settings → environment → Allowed domains] (a custom cloud environment is needed; the built-in one has no settings).

## Contracts (hookr-1, Robinhood Chain 4663)
| Contract | Address | Role for MILORD |
|---|---|---|
| HookrLauncher | 0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4 | `launch(Token{existing,...}, root, Member[], deadline)` opens the existing-token family and holds the position |
| HookrFamilyLock | 0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b | `lock(familyId, beneficiary, unlockBlock)`, `collect(familyId, member, claims)`, `release` |
| HookrRoot (hook) | 0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc | the v4 hook of every Hookr pool |
| HookrRules | 0x4a71c270b1520F94064268102AebD744b0DE7942 | rules (snipe tax, caps, burn/lp/pot/royalty bps, protocol share) |
| HookrFamilyRouter | 0x854f3Fa7AC342B244DF0d9A8c8F597b051aAE559 | authenticated swap router |
| HookrLiquidityVault | 0xA408923EF811945Ef119Bc700B1950Dab65467Fc | |
| HookrFeeAdvisory | 0x3B6b…7306 | keeper-repriced fee advisory: **not** the Tax + Conversion block |
| USDG | 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168 | vault asset (address confirmed in Hookr docs) |

## Item by item
| Requirement | Finding | Status |
|---|---|---|
| Existing-token launcher | `launch` takes `Token{existing, name, symbol, supply, salt}`; existing MILORD address is supported by the struct | ABI-compatible; **fork test needed** |
| 100% supply in one MILORD-only position, 0 ETH | `Member{tickLower, tickUpper, liquidity, amount0Max, amount1Max, sqrtPriceX96}` lets us pass amount0Max = 0 and sqrtPrice at tickUpper | ABI-compatible; **fork test needed** that the launcher accepts amount0 = 0 and pulls exactly 6,766,999,999.999999999999975080 MILORD |
| Opening price (~10 ETH FDV, tick 203280) | `sqrtPriceX96` is a launch field; docs' "Opening check" only compares later pools to the first one | compatible |
| Ticks 134220–203280, spacing 60 | both multiples of 60; within v4 bounds | compatible |
| Flat 1.5% tax from trade one, in ETH | Tax + Conversion is an advisory block; its address and config bytes are **not published**. `RulesConfig` has `snipeTaxPips`, `snipeFloorPips`, `snipeDecaySeconds`, `guardEndBlock`, `maxBuyQuoteAmount`: all must be 0 for no surcharge / no cap | **BLOCKER B1** (cannot encode or prove) |
| Commission 20% | likely `protocolShareBps` in RulesConfig or `quoteTakePips` in the advisory; not confirmed | **BLOCKER B2** |
| LP fee 0.3% | `PoolConfig.baseLpFeePips = 3000`, with `caps.maxLpFeePips`; the advisory can add `lpFeeSurchargePips` | compatible if surcharge is 0; fork check |
| Other Hook Blocks | burn/lp/pot/royalty bps and recapture must be 0 / off | fork check |
| Permanent lock | HookrFamilyLock: owner hands the family over (`transferFamily`), then `lock(familyId, beneficiary, unlockBlock)`; docs: "forever" never releases, the beneficiary cannot be changed, "the lock blocks withdrawals only, fees remain collectable" | compatible. Exact "forever" value (we use 2^64−1), `transferFamily` `claims` argument and whether `lock` performs the `acceptFamily` step: **fork check** |
| LP fees into BuybackBurner | only the beneficiary may `collect`; a contract can be the beneficiary. Our **HookrLockFeeCollector** is that beneficiary: anyone can trigger it, ETH goes to BuybackBurner, MILORD is burned; if Hookr pays PoolManager claims (`claims` > 0) it redeems them first | compatible by ABI; whether `collect` pays `msg.sender` or holds claims: **fork check** |
| First buy, later buy, sell | need the HookrRoot / HookrFamilyRouter swap path | **fork test needed** |
| Buybacks on the Hookr pool | HookrRoot stores router/quoter code hashes and has an "authenticated" swap flag and `Unauthorized` / `UnauthenticatedRefund` errors. A direct PoolManager swap by our V4SwapAdapter may be refused or treated differently | **BLOCKER B3**: if refused, BuybackBurner's adapter must route through HookrFamilyRouter (a new adapter, same burner) |

## Lock decision (no competing implementations)
Hookr holds launch positions inside its launcher as families, not as Uniswap PositionManager NFTs, so our earlier `MilordLiquidityLock` (PositionManager-based) cannot hold a Hookr launch position. It was removed. We use Hookr's reviewed **HookrFamilyLock** and add only the smallest adapter it needs, **HookrLockFeeCollector** (≈90 lines, no owner). `MockHookrFamilyLock` exists only under `src/mocks` to test the collector against real v4 fee accounting.

## What a fork run must do (script outline, ready once RPC is reachable)
1. Fork 4663; deploy MILORD + protocol (DeployRobinhood refuses 4663 broadcast; fork only).
2. `launch` the existing-token family with the config above; assert MILORD pulled, 0 ETH, tick 203280.
3. Buy 1 ETH, buy 0.1 ETH, sell; assert tax = 1.5% of ETH each time, commission share, no surcharge.
4. `transferFamily` + `lock(…, collector, FOREVER)`; assert `withdraw` reverts for every role.
5. Trade, `collector.collect(0)`; assert ETH reached BuybackBurner, MILORD supply fell by the MILORD fee, position liquidity unchanged.
6. `BuybackBurner.buybackAndBurn` through V4SwapAdapter; if HookrRoot refuses, switch to a HookrFamilyRouter adapter.
