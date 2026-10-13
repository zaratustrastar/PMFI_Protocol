# pArbitrage integration

MILORD reuses the existing pARB design end to end. It adds no new arbitrage accounting.

## What is reused unchanged

- The vault interface (`requestDeposit`, `claimDeposit`, `autoClaimDeposits`, `requestRedeem`, `claimRedeem`,
  `autoClaimRedeems`, `cancelRequest`, `tend`, `report`, previews, request getters).
- The `ReportDataV2` struct and EIP-191 signing (new domain salt `PMFIArbVaultV3.milord.v1` so V2 reports
  cannot be replayed).
- The arbitrage engine, servicer wallet and venue accounts.
- The frontend's deposit/redeem flow (address change only).

## What changes (PMFIArbVaultV3)

| Fix | Why |
|---|---|
| V3-1 perf fee constructor param, 0 for MILORD | spec; V2 fee is a constant 20% |
| V3-2/3 outstanding shares = circulating + locked-for-redeem + processed-unclaimed | V2 PPS inflation bugs |
| V3-4 zero NAV reverts | no fake $1 |
| V3-5 report ≥ vault USDC; PPS move ≤ 10% per report unless owner pre-approves that nonce | single-signer blast radius |
| V3-6 optional depositor allowlist (only the adapter for MILORD) | keeps fee-funded instance clean |
| V3-7 emergency withdraw limited to unreserved USDC, shutdown only | V2 could take reserved funds |
| V3-8 report runs in shutdown (redeems only), deposits cancellable | orderly exit |

## Separate instance, not the existing V2

Recommended: deploy V3 as a **separate** vault for MILORD fees. Reasons: V2's 20% fee cannot be turned off,
its PPS bugs affect every holder, and mixing fee-funded deposits with outside depositors would make
MILORD holders' pARB share the V2 fee. The engine can run both vaults; the reporter needs per-vault NAV
attribution (each vault's servicer allocation tracked separately). That attribution is the main reporter change.

If you prefer to reuse the existing V2 instance anyway, `ParbFundingAdapter` works with it unchanged
(same interface), but holders would pay V2's 20% fee and be exposed to V2-2/V2-3. Not recommended.

## Required reporter fixes before mainnet

1. Mark open positions consistently (either the stated $0.99 convention or a documented market mark), and
   apply the haircut that is declared but unused, or delete it.
2. Count in-flight venue transfers.
3. Value one-legged positions at market (bid), not cost.
4. Report per vault instance.
5. Keep the reconciliation log the V3 PPS guard expects: any report moving PPS > 10% needs an owner
   `approveLargeMove(nonce)` with a written explanation.

## Data honesty

No profit or PPS figures are produced by this repository. The simulation uses labelled scenario inputs and
mocks; the dashboard says so on every view.
