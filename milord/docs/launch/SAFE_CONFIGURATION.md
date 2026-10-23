# Safe configuration and permissions (MILORD V1)

Status: testnet-ready configuration for independent security review. Nothing here has been deployed or executed on Robinhood Chain (4663). The permanent LP lock is approved as architecture but **must not be executed** until the owner gives a separate go-ahead.

## 1. Safes and keys

| Holder | Type | Recommended threshold | Holds funds? |
|---|---|---|---|
| **Team Safe** | Safe multisig | 3-of-5 (owner to confirm signers) | Team share of revenue (ETH) |
| **Arbitrage Funding Safe** | Safe multisig | 2-of-3 minimum | Nothing in normal operation: revenue flows to the router. Receives only swept funds. |
| **Allocation / launch Safe** | Safe multisig (can be the Team Safe) | as Team Safe | 100% of MILORD for one transaction (the Hookr launch); 0 afterwards |
| Keeper key | EOA, gas only | n/a | ~0.05 ETH gas |
| Publisher key | EOA, gas only | n/a | gas |
| Report signer | KMS/HSM key, signs vault reports | n/a | none |
| Quote signer | KMS/HSM key, signs entry quotes | n/a | none |
| Monitor key | EOA, gas only | n/a | gas |
| Engine servicer wallet | existing arbitrage engine wallet | existing | trading capital from `tend()` (existing V2 model) |

No bot or keeper key is a signer or module on any Safe. No Safe module, delegate call or allowance module is used.

## 2. Contract permissions

### Team Safe (owner of everything except the router)
| Contract | Owner-only powers | Cannot |
|---|---|---|
| MilordsNFT | set converter (once), royalty, base URI, freeze metadata | mint or move NFTs |
| MilordConverter | lower fees now / raise fees after timelock, quote signers, quote lifetime, pause entry, entry-fee recipient (timelocked), rescue non-MILORD tokens | touch escrowed MILORD (`rescueERC20` refuses MILORD); stop redemptions |
| RevenueSplitter x3 | shares within fixed min/max bounds, payee change (timelocked queue/execute) | take funds already allocated |
| BuybackBurner | operator list, min/max buyback size, swap adapter (initial once, then timelocked) | withdraw ETH or MILORD (only buyback and burn) |
| StrategyRegistry | characteristics, strategies, defaults | anything with funds |
| PMFIArbVaultV3 | fee recipient (fee is 0%), servicer wallet, cap, idle/tend/report cooldowns, max PPS move and large-move approval, depositor allowlist, pause, shutdown; `emergencyWithdraw` **only after shutdown** | change PPS directly; mint shares |
| ParbFundingAdapter | operator list (the router; optionally the keeper for manual batches), rescue stray tokens | deposit for anyone but the distributor |
| ShareDistributor | **veto a pending root** during the 1-day delay, publisher key, activation delay (1 h to 7 d), rescue non-pARB tokens | withdraw pARB (`rescue` refuses it); change claims of an active root |

### Arbitrage Funding Safe (owner of ArbitrageFundingRouter)
| Power | Bound |
|---|---|
| `setParams(min, max, daily, slippage, interval)` | slippage <= 5% hard cap; min <= max <= daily; interval <= 7 days |
| `setRoute(venue, priceReference)` | any contract: treat as a high-risk change and review each time |
| `setKeeper`, `setOpenExecution`, `setPauser` | |
| `pause` / `unpause` | pausers can only pause |
| `sweep(token)` | pays **only to the Safe itself** |

### Keys
| Key | Can call | Worst case if stolen |
|---|---|---|
| Keeper (BuybackBurner operator) | `buybackAndBurn` within the burner's min/max and a caller-supplied minimum output; everything else it calls is permissionless | buyback at a bad price within one max-size buyback; mitigate with the burner max and by removing the operator |
| Publisher | `ShareDistributor.proposeRoot` | a wrong root, caught by the root checker and vetoed by the Team Safe within 1 day; it can never exceed pARB held (Underfunded check) |
| Monitor | `ArbitrageFundingRouter.pause` | pauses funding (no loss) |
| Report signer | vault `report` (bounded by max PPS move and cooldown) | wrong PPS within the bound; large moves need Team Safe approval |
| Quote signer | entry quotes for the converter | mispriced entry fee within quote lifetime |
| Router (contract) | `ParbFundingAdapter.fund` with USDG it just delivered | nothing beyond its bounded swap |

### No-owner contracts
- **HookrLockFeeCollector**: no owner, no setter, no withdraw. `collect`, `forward` and `redeemClaims` are permissionless and only pay ETH to BuybackBurner and burn MILORD.
- **UniV3SwapVenue, UniV3TwapReference, ChainlinkReference, DualReference**: immutable.
- **HookrFamilyLock** (Hookr's): after `lock(familyId, collector, FOREVER)` nobody, including the Team Safe and Hookr's own contracts, can withdraw the founding position; only the collector can collect fees.

## 3. Router production values (proposal, for the owner to confirm)
| Parameter | Value | Why |
|---|---|---|
| minSwapWei | 0.05 ETH | below this the gas and the vault's $10 minimum dominate |
| maxSwapWei | 2 ETH | well under 1% price impact on a WETH/USDG pool with >$1M depth (to be checked on chain) |
| dailyCapWei | 10 ETH | caps exposure if the price reference were wrong for a day |
| maxSlippageBps | 100 (1%) | below the 30-min TWAP |
| minInterval | 1 hour | spreads swaps; one manipulation window per hour at most |
| openExecution | true | anyone can trigger; all bounds are on chain |
| TWAP window / max spot deviation | 30 min / 100 ticks (~1%) | refuses same-block manipulation and missing history |
| Pauser | Monitor key | |

## 4. Safe transactions (encoded by `offchain/safe-launch-batch.mjs`, never sent by it)
1. **Team Safe**: `acceptOwnership()` on every Ownable2Step contract, `BuybackBurner.setInitialAdapter(V4SwapAdapter)`.
2. **Arbitrage Funding Safe**: `ArbitrageFundingRouter.acceptOwnership()`.
3. **Team Safe**: `MilordAtomicLaunch.launch{value: Hookr launch fee}(TradingTaxSplitter, deadline)` (`safe-1-launch.json`): creates MILORD, launches it with 100% of supply, burns the rounding dust and verifies every frozen term in one transaction; then `safe-2-accept.json` (`acceptFamily`, `sweep`, `setInitialAdapter`). There is no allocation Safe: no wallet ever holds MILORD outside the pool.
4. **Team Safe (family owner), DO NOT EXECUTE until approved**: `HookrLauncher.transferFamily(familyId, HookrFamilyLock, 0)`, `HookrFamilyLock.lock(familyId, HookrLockFeeCollector, 2^64-1)`.

## 5. Emergency playbook
| Event | Who | Action |
|---|---|---|
| Price reference or venue misbehaving | Monitor key or Arbitrage Safe | `router.pause()`; revenue accumulates safely in the router |
| Root checker critical alert | Team Safe | `ShareDistributor.vetoPending()` before activation |
| Bad report / engine incident | Team Safe | `vault.setPaused(true)`; shutdown only for unrecoverable cases |
| Keeper key compromised | Team Safe | `BuybackBurner.setOperator(key, false)`; anyone can run the permissionless steps meanwhile |
| Entry pricing incident | Team Safe | `converter.setEntryPaused(true)` (redemptions keep working) |
