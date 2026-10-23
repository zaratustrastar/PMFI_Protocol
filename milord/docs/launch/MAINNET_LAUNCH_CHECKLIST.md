# Mainnet launch checklist

Tick every line before any 💸 transaction. Status today in brackets.

## Owner approvals [NEEDS APPROVAL]

- [x] Opening FDV 10 ETH, 0 ETH seeded, MILORD-only position (Bobo, 2026-10-14)
- [x] 100% of supply (6,767,000,000 MILORD) in the founding position, 0 outside; no migration reserve (Bobo, 2026-10-14, final)
- [ ] Range top ≈1,000× (ticks 134220–203280), "subject to technical feasibility": feasible, confirm
- [ ] LP fee (0.3% proposed)
- [x] Lock architecture approved: permanent HookrFamilyLock, beneficiary HookrLockFeeCollector (LP_LOCK.md)
- [ ] Owner go-ahead to EXECUTE the lock (irreversible)
- [ ] Hookr fork verification passes (HOOKR_VERIFICATION.md; BLOCKERS.md B1-B5)
- [ ] Safe signers, thresholds and router bounds confirmed (SAFE_CONFIGURATION.md)
- [ ] Services deployed on testnet with alerts wired (offchain/services/README.md)
- [ ] ArbitrageFundingRouter as the arbitrage payee, its caps and slippage (PARB_AUTOMATION.md)
- [ ] Deployment gas and transaction expenses (explicit approval required)
- [ ] Hookr commission (20%, the minimum, proposed)
- [ ] Launch-tax handling (R-01): decay = 0 if flat; anything above 1.5% needs explicit approval
- [ ] Tax recipient: TradingTaxSplitter instead of Revenue Router (proposed)
- [x] Kalshi kept; Opinion disabled for V1, adapter preserved (Bobo, 2026-10-14)
- [ ] Mainnet deployment itself

## Hookr [EXTERNAL]

- [ ] Verified hookr-1 source/ABI for HookrLauncher and HookrRoot read
- [ ] Existing-token launch accepts our complete supply as a MILORD-only seed with 0 ETH (or use path B)
- [ ] On the real contracts: exactly 6,767,000,000 MILORD (minus rounding, burned) enters the position; the deployer/Safe
      ends with 0; opening FDV ≈10 ETH; 0 ETH in the position
- [ ] First buy succeeds; later buys and sells execute; tax is 1.5% from the first transaction
- [ ] Hookr commission and the three fee destinations settle correctly; a buyback buys and burns MILORD
- [ ] **Required by Bobo before deployment:** the actual Hookr contracts charge exactly 1.5% from the first trade, with no higher launch surcharge (decay = 0 or equivalent)
- [ ] Live commission floor and `launchFee()` read
- [ ] Tax payout asset (ETH or WETH) and payout function confirmed
- [ ] Direct v4 swaps from BuybackBurner/V4SwapAdapter accepted by the hook (else HookrRouter route)
- [ ] Discover visibility for existing-token launches
- [ ] After launch: block list shows only Tax + Conversion; Anti-Snipe, dynamic fees, LP rewards, Auto Burn,
      recapture all off; tax 1.5/1.5; recipients = splitter; lock as approved

## Assets and routes [EXTERNAL]

- [ ] USDG address, 6 decimals and bytecode checked on the 4663 explorer
- [ ] WETH/USDG V3 pool depth checked on the day; swap route for the Arbitrage Safe chosen
- [ ] Polymarket USDG deposit on 4663 confirmed with a small amount (approval needed: real funds)
- [ ] Polymarket withdrawal destination confirmed
- [ ] Funding bot changes (ARBITRAGE_FUNDING_READINESS §3) implemented, NAV counts in-transit, tested small

## Contracts [TESTED locally, not audited]

- [x] 139 Foundry tests pass (100% allocation)
- [x] 36/36 end-to-end checks pass (100% allocation)
- [ ] Robinhood fork test of the full path passes
- [ ] Testnet dry run done
- [ ] External audit, findings resolved
- [ ] Sources verified on the explorer after deployment

## Automation (PARB_AUTOMATION.md)

- [ ] ArbitrageFundingRouter built, tested, set as arbitrage payee in the three splitters
- [ ] Production epoch job (finalized blocks, IPFS data, `proposeRoot`) and independent root watcher
- [ ] Keeper for the permissionless steps (Hookr payout, distribute, settle, activateRoot, burnHeldTokens, collectFees)

## Operations

- [ ] Safes created with agreed signers (allocation, Team, Arbitrage)
- [ ] Quote signer key in KMS/HSM; report signer separate; operators separate
- [ ] Quote service deployed with production settings from `offchain/lib/entry-price.mjs` (30 min / 120 s / 10% / ticks 134220–203280 / min liquidity 1e23 / opening tick 203280), ticks re-checked against the real Hookr pool
- [ ] Epoch job and second-person root check scheduled
- [ ] Monitoring: tax payouts, splitter balances, buybacks, vault reports, USDG pause events, bridge arrivals
- [ ] Incident contacts (Hookr, Paxos, venues)

## Website [BUILT, NOT INTEGRATION-TESTED on 4663]

- [x] Connect, network switch, balances, buy/sell disclosure, convert, redeem, characteristics, strategy, pARB
      PPS and balance, claims, governance (UI test passes locally)
- [ ] Official Hookr pool URL set (`?hookr=`)
- [ ] Marketplace URL set (`?market=`)
- [ ] Mainnet and testnet RPC/explorer values verified
- [ ] Outdated copy removed on the existing site (`docs/CURRENT_PROJECT_AUDIT.md` §5)
- [ ] Disclosures: tax 1.5% buy/sell (Hookr keeps 20%), LP fee, minimum entry fee, royalties not guaranteed,
      pARB not instantly sellable, no promised return, unaudited until audited
