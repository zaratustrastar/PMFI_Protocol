# Security findings and risk register

Contracts are **unaudited** and not production-ready until audited and fork-tested.

| ID | Risk | Severity | Mitigation | Status |
|---|---|---|---|---|
| R-01 | Hookr "tax at launch" must exceed the base tax; could breach the 1.5% ceiling | High | use decay = 0 if it keeps the tax flat; otherwise owner decision | EXTERNAL, open |
| R-02 | Hookr core blocks on by default (Anti-Snipe 59.7%, dynamic fees 2.3%, Auto Burn, LP rewards) | High | switch all off at launch; verify the pool's block list after launch, before announcing | checklist |
| R-03 | Anyone can open a MILORD pool on Hookr | Medium | publish only the official pool id; website links only it | checklist |
| R-04 | Hookr hook may reject direct v4 swaps from BuybackBurner / V4SwapAdapter | Medium | fork test; else route via HookrRouter (adapter change) | EXTERNAL, open |
| R-05 | Tax payout fails → recipient gets ERC-6909 claims the splitter cannot redeem | Medium | splitter `receive()` never reverts; monitor payouts | TESTED locally |
| R-06 | Token-only pool: sellers drain buyers' ETH; insiders outside the pool can dump | High | allocation lockups; disclose that early sellers may get little ETH | NEEDS APPROVAL |
| R-07 | USDG is upgradeable, pausable, freezable, wipeable by Paxos | Medium | accept issuer risk; monitor; pause deposits on a pause event | EXTERNAL |
| R-08 | USDG/USDC peg drift during bridging distorts NAV | Medium | peg haircut and in-transit accounting in `arb_nav` | to implement |
| R-09 | NAV misses in-transit funds | High | add pending bridge amounts (ARBITRAGE_FUNDING_READINESS §4) | to implement |
| R-10 | Kalshi $2,500/day withdrawal cap locks capital | Medium | owner decides whether Kalshi stays; size positions to the cap | NEEDS APPROVAL |
| R-11 | Entry pricing manipulated by spot moves | High | 30-min TWAP, 30-min history, 10% spot deviation, 120 s staleness, no fallback | TESTED |
| R-12 | Oracle/quote outage makes the 0.002 ETH minimum exploitable | High | no fallback: conversions stop instead | TESTED |
| R-13 | Quote-signer key compromise | High | signer key in a KMS/HSM; short quote validity; owner can revoke and rotate signers (`setQuoteSigner`) | process |
| R-14 | Buybacks sandwiched | Medium | minOut from TWAP, price limit, threshold, allow-listed operators, per-buy min/max | TESTED |
| R-15 | Buybacks pay the 1.5% tax | Low | accepted; ⅓ of burn budget's tax returns to the splitter | accepted |
| R-16 | Merkle root wrong or unfunded | High | roots only when funded; allocated ≤ minted invariant; publish data for anyone to verify | TESTED |
| R-17 | Royalties ignored by marketplaces | Low | never assumed; reconciliation counts only what arrives | TESTED |
| R-18 | pARB not instantly sellable / redeemable | Medium | disclose; vault withdrawals follow V3 rules | disclosure |
| R-19 | Reported PPS wrong (reporter error or compromise) | High | V3 report bounds, signer separation, reconciliation dashboard | existing V3 |
| R-20 | LP position locked forever: no recovery if Hookr pool is broken | Medium | owner decides lock type (forever vs until block) | NEEDS APPROVAL |
| R-21 | Website shows a stale or wrong network | Low | network switch to 4663; testnet entries marked unverified | TESTED locally |
| R-22 | Third-party bridges (Across, Relay, LI.FI) fail or are exploited | Medium | cap per transfer; one route at a time; monitor arrivals | EXTERNAL |
| R-23 | Opinion collateral token mismatch (bot uses BSC USDC, docs say USDT) | Medium | confirm before funding | EXTERNAL |

## Findings fixed during this pass

- V4SwapAdapter refund path would double-count ETH at the burner: replaced with a "partial fill" revert.
- Lock reported "not locked" because PositionManager mints without a callback: `isLocked` is now ownership-based
  and `register()` records the position.
- BuybackBurner adapter could be repointed at will: `setInitialAdapter` works once, then only the delayed path.
