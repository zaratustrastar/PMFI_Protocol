# Mainnet launch checklist

Tick every line before any 💸 transaction. Status today in brackets.

## Owner approvals [NEEDS APPROVAL]

- [ ] Pool share of supply, opening price/FDV, range (TOKEN_ALLOCATION.md)
- [ ] Remainder allocation and lockups (team, treasury, community, legacy PMFI)
- [ ] LP fee (0.3% proposed)
- [ ] Lock type: forever (proposed) or until a block; LP fee recipient (Team Safe proposed)
- [ ] Hookr commission (20%, the minimum, proposed)
- [ ] Launch-tax handling (R-01): decay = 0 if flat; anything above 1.5% needs explicit approval
- [ ] Tax recipient: TradingTaxSplitter instead of Revenue Router (proposed)
- [ ] Kalshi kept or dropped given the $2,500/day withdrawal cap
- [ ] Mainnet deployment itself

## Hookr [EXTERNAL]

- [ ] Verified hookr-1 source/ABI for HookrLauncher and HookrRoot read
- [ ] Existing-token launch accepts a MILORD-only seed with 0 ETH (or use path B)
- [ ] decay = 0 keeps the tax flat at 1.5%
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
- [ ] Opinion collateral token confirmed
- [ ] Funding bot changes (ARBITRAGE_FUNDING_READINESS §3) implemented, NAV counts in-transit, tested small

## Contracts [TESTED locally, not audited]

- [x] 134 Foundry tests pass
- [x] 35/35 end-to-end checks pass
- [ ] Robinhood fork test of the full path passes
- [ ] Testnet dry run done
- [ ] External audit, findings resolved
- [ ] Sources verified on the explorer after deployment

## Operations

- [ ] Safes created with agreed signers (allocation, Team, Arbitrage)
- [ ] Quote signer key in KMS/HSM; report signer separate; operators separate
- [ ] Quote service deployed with production TWAP settings (1800/1800/120 s/10%)
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
