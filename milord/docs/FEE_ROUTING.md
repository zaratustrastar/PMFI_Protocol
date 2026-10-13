# Fee routing

All routing uses `RevenueSplitter`, one instance per stream. `receive()` only emits an event, so
marketplaces paying royalties with a 2,300-gas stipend succeed. `distribute(token)` is permissionless; a
payee that rejects a push is credited for a later `withdraw`, so one bad payee cannot block the others.

| Stream | Instance | Payees (bps) | Bounds |
|---|---|---|---|
| Entry fee (ETH) | `EntryFeeSplitter` | Arbitrage Safe 7000, Team Safe 3000 | set at deployment; recommended Arbitrage [6000, 8000] |
| Royalties | `RoyaltySplitter` | Team 5000, Arbitrage 5000 | recommended [4000, 6000] |
| Trading tax | `TradingTaxSplitter` | BuybackBurner 3334, Team 3333, Arbitrage 3333 | recommended each [2500, 4000] |

Local deployment uses [0, 10000] bounds for testing. **Production bounds are an open decision**; once set
they cannot be widened.

## Admin controls

- Share changes: immediate but only inside per-payee bounds fixed at construction. Previously received ETH is
  split at the old shares first.
- Payee address changes: queued, 2-day delay, cancellable.
- No admin can take funds from a splitter; there is no sweep function.

## Launchpad commission

If the launch venue keeps a commission from the 1.5%, the splitter only sees what arrives, so the three
shares shrink proportionally with no contract change. Example: 1.5% tax with a 0.3% venue cut leaves 1.2%,
i.e. 0.4% each.

## Bridging to Base

The Arbitrage Funding Safe bridges ETH to Base and swaps to USDC through ordinary Safe transactions; no
contract holds funds in transit. Each batch is tagged with a `sourceRef` (hash of the splitter allocation
events and the bridge/swap transactions) that `ParbFundingAdapter` accepts only once, so the same fees cannot
be turned into shares twice. The Team Safe's share is never deposited.

## Buyback and burn

`BuybackBurner` holds ETH budget. An operator executes `buybackAndBurn(ethIn, minOut, deadline)`; the
contract checks the MILORD received is ≥ `minOut`, burns all of it and stores an auditable record. Budget
received and MILORD actually burned are tracked separately. The swap adapter is a thin wrapper around the
venue's router (Universal Router on Robinhood Chain); replacing it takes a 2-day delay.
