# Share accounting (PMFIArbVaultV3)

Units: USDG 6 decimals (the constructor rejects any other asset decimals); shares 18 decimals; PPS = USDG per 1e18 shares (so $1.00 = 1,000,000).

## Report

```
backing      = reportedAssets − pendingDepositAssets − claimableRedeemAssets
outstanding  = totalSupply − deadShares + unclaimedProcessedDepositShares
fee          = perfFeeBps × max(0, backing − HWM)            (0 for MILORD)
PPS          = (backing − fee) / outstanding
```

- Shares locked for a pending redemption remain in `totalSupply` (held by the vault) and therefore in
  `outstanding`; their assets remain in `backing`. Consistent numerator and denominator.
- Deposits processed in this report get shares at this PPS and add to `unclaimedProcessedDepositShares` until
  claimed. Their assets leave `pendingDepositAssets`, so they enter `backing` from the next report. Both
  sides move together.
- Redemptions processed at this PPS move assets to `claimableRedeemAssets` and burn the shares.
- First deposit into an empty vault: PPS $1.00. Afterwards PPS always comes from the formula.

## Guards

| Guard | Rule |
|---|---|
| on-chain cash | `reportedAssets ≥ usdc.balanceOf(vault)` |
| zero NAV | revert if PPS would be 0 with shares outstanding |
| PPS move | `|ΔPPS| ≤ maxPpsMoveBps` (default 10%) unless `approveLargeMove(nonce)` |
| freshness | signed, nonce strictly increasing, deadline, ≤ 1 h stale |

## Invariants (tested)

- `usdc.balanceOf(vault) ≥ pendingDepositAssets + claimableRedeemAssets`
- `unclaimedProcessedDepositShares == Σ shares of CLAIMABLE deposit requests`
- after each report, `PPS ≈ lastReportedBackingAssets / outstanding` (rounding ≤ 2 units)
- depositing never changes PPS; a depositor's shares are worth ≤ what they paid at that PPS

## Deposited capital is not revenue

Deposits raise the HWM and are excluded from profit; with a 0% fee nothing is taken in any case. Fee USDG
deposited by the adapter buys shares at the current PPS like any deposit, so it neither raises PPS for
existing holders nor gets counted as profit.
