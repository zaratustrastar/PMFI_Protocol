# Fee routing verification

Every ETH stream, where it goes, and how it was checked. Numbers are from the local end-to-end simulation (three
weeks, illustrative trades). SIMULATED: no real trades or revenue.

## Streams

| Stream | Rate | Paid by | Path | Status |
|---|---|---|---|---|
| Trading tax | 1.5% buy, 1.5% sell, in ETH | traders | Hookr Tax + Conversion → (Hookr keeps commission) → TradingTaxSplitter → ⅓ BuybackBurner / ⅓ Team Safe / ⅓ Arbitrage Safe | SIMULATED WITH MOCKS (hook), splitter TESTED; EXTERNAL for real Hookr |
| Hookr commission | 20% of tax (0.30% of volume) | from tax | Hookr treasury | EXTERNAL |
| LP fee | 0.3% | traders | accrues to the locked position; `HookrLockFeeCollector.collect` sends ETH to BuybackBurner and burns MILORD | TESTED (real v4, mock Hookr lock) |
| NFT entry fee | max(10% of 1M MILORD value, 0.002 ETH) | converters | MilordConverter → EntryFeeSplitter → 70% Arbitrage Safe / 30% Team Safe | TESTED |
| Exit burn | 100,000 MILORD per redemption | redeemer | `burn()` in MilordRedemptionEscrow | TESTED |
| Royalty | 6.7% when the marketplace honours ERC-2981 | buyer/marketplace | RoyaltySplitter → 50% Arbitrage Safe / 50% Team Safe | TESTED (paid and unpaid cases) |
| Arbitrage funding | all ETH reaching the Arbitrage Safe | — | ETH → USDG (V4SwapAdapter locally; Uniswap V3 WETH/USDG on Robinhood) → ParbFundingAdapter → V3 vault deposit → pARB → distributor | TESTED locally; route on Robinhood EXTERNAL |
| Buyback burn | burner's ⅓ of tax | — | BuybackBurner at threshold, minOut + price limit, `MILORD.burn`, `BuybackBurned` event | TESTED |

## Simulation reconciliation (3 weeks, final 100% configuration)

| Item | ETH |
|---|---|
| Taxed volume | 58.233260 |
| Buy tax | 0.530206 |
| Sell tax | 0.351964 |
| Total tax (1.5% of volume) | 0.882170 |
| Hookr commission (20%) | 0.176395 |
| Project share (80%) | 0.705580 |
| → BuybackBurner | 0.235240 |
| → Team Safe | 0.235170 |
| → Arbitrage Safe | 0.235170 |
| Entry fees | 0.235980 (Arbitrage 0.165186 / Team 0.070794) |
| Royalties (one honoured sale) | 0.033500 (50/50) |
| LP fees (separate, stay with the position) | 0.104440 ETH + 6,520,352.8 MILORD |
| Arbitrage ETH swapped to USDG | 0.417106 → 1,042.144247 USDG → 1,033.080 pARB |
| Burn budget spent | 0.235240 → 34,576,904.54 MILORD burned |

The burner's share is 0.03 bp higher than the other two because the splitter assigns rounding dust to the first
payee (33.34%). Checked within 1 bp.

## Automated checks (all pass)

- tax = 1.5% of taxed ETH volume (buys and sells)
- Hookr commission == 20% of tax; project == 80% (fuzzed 20–50% in Foundry)
- project tax fully received by TradingTaxSplitter
- burn / team / arbitrage shares equal within 1 bp
- entry fees: converter total == splitter total, 70/30
- royalty received only when paid, split 50/50
- all arbitrage ETH swapped to USDG and deposited
- buyback burns recorded == supply reduction from buybacks
- LP fees collected separately from tax

## Rules enforced

- Deposits are never revenue: the week-1 deposit left PPS at exactly 1.000000.
- Each fee batch is deposited once (`ParbFundingAdapter` batch id; second attempt reverts, checked).
- The team has no path to the redemption escrow (no owner withdraw; Foundry test).
- Burns are true `burn()`; no dead-address transfers; Hookr Auto Burn off.
- Buybacks pay the 1.5% tax like any buy (part of the burn budget returns to the splitter as tax). Accepted
  consequence of an immutable tax; noted in the risk register.

## Open with real Hookr

1. Payout asset (ETH vs WETH) and the settle/pay-out call names. The splitter accepts both.
2. Whether the hook accepts the BuybackBurner's direct v4 swap or requires HookrRouter.
3. Live commission floor.
