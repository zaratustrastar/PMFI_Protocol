# Mainnet deployment runbook

**Do not execute without written approval.** Contracts are unaudited. This runbook spends no funds by itself;
every step that does is marked 💸 and needs explicit sign-off. Secrets are never stored in the repo: use a
hardware wallet or Safe for every owner action and pass deployer keys via `--ledger`/`--account`, never env files in git.

## 0. Prerequisites

- [ ] External audit complete, findings fixed
- [ ] D1–D8 in OPEN_DECISIONS resolved
- [ ] Team Safe and Arbitrage Funding Safe deployed on Robinhood Chain and Base (same signers)
- [ ] Quote-signer and report-signer keys generated in their services; addresses recorded
- [ ] Robinhood Chain RPC with `finalized` tag support
- [ ] Reporter fixes merged (PARBITRAGE_INTEGRATION)
- [ ] Testnet rehearsal (Robinhood testnet 46630 + Base Sepolia) of every step below

## 1. Robinhood Chain (id 4663)

1. Deploy `MILORDToken(initialHolder = allocation Safe)` — check `totalSupply == 6,767,000,000e18`.
2. Deploy splitters with production bounds:
   royalty (Team 5000 / Arbitrage 5000), entry (Arbitrage 7000 / Team 3000).
3. Deploy `MilordsNFT(owner = Team Safe, royaltyReceiver = RoyaltySplitter, baseURI)`.
4. Deploy `MilordConverter(owner = Team Safe, token, nft, EntryFeeSplitter, quoteSigner)`.
5. From Team Safe: `nft.setConverter(converter)` (one-time).
6. Deploy swap adapter for the chosen pool (D1), `BuybackBurner(owner = Team Safe, token, adapter)`;
   Safe: `setOperator(opsKey)`.
7. Deploy `TradingTaxSplitter` (Burner / Team / Arbitrage).
8. Deploy `StrategyRegistry(Team Safe, nft)`; Safe: add 8 characteristics, add strategy "pArbitrage"
   (chainId 8453, vault, vault), `setDefaultStrategy`.
9. Deploy `BasePayoutRegistry`, `MilordGovernor(nft)`.
10. Verify all sources on the explorer; record addresses in `deployments/robinhood-mainnet.json`.

## 2. Base (id 8453)

1. Deploy `PMFIArbVaultV3("PMFI Arb (MILORD)", "pARB-M", USDC 0x8335…2913, reportSigner, feeRecipient = Team Safe, servicer, cap, 0)`.
2. Deploy `ShareDistributor(owner = Team Safe, vault, publisher)`.
3. Deploy `ParbFundingAdapter(owner = Team Safe, USDC, vault, distributor)`.
4. Safe on vault: `setDepositorRestricted(true)`, `setAllowedDepositor(adapter, true)`, review idle/cooldown params,
   `transferOwnership(Team Safe)` (V3 uses Ownable; deployer → Safe).
5. Safe on adapter: `setOperator(opsKey)`. Safe on distributor: `setActivationDelay(1 days)`.
6. Verify; record in `deployments/base-mainnet-milord.json`.

## 3. Launch sequence

1. 💸 Create the pool chosen in D1 and seed liquidity from the allocation Safe.
2. Start the quote service pointed at the pool TWAP; test a quote end-to-end with a team wallet.
3. 💸 First conversion by a team wallet; check escrow, fee split, NFT, votes.
4. Freeze metadata once art is final.
5. Enable the frontend.

## 4. Weekly operations

1. `distribute` all three splitters; execute buybacks within limits.
2. 💸 Arbitrage Safe: bridge ETH → Base, swap → USDC, send to adapter.
3. Ops: `adapter.fund(sourceRef, amount)` (sourceRef = hash of the splitter allocation logs and bridge/swap txs).
4. Next vault report; `adapter.settle(batchId)`.
5. After Monday 00:00 UTC + finality margin: run the epoch job, publish `epoch-N.json` (IPFS), `proposeRoot`.
6. During the activation delay, a second person recomputes the root from public data; veto if it differs.
7. Frontend shows new claims.

## 5. Emergency

| Situation | Action |
|---|---|
| Bad quote signer | `setQuoteSigner(addr, false)`; `setEntryPaused(true)` if needed. Exits keep working. |
| Fee too high | `reduceFeeParams` (immediate) |
| Bad report | vault `setPaused(true)`; investigate; reports moving PPS > 10% already revert |
| Bad root | `vetoPending()` during the delay |
| Vault wind-down | `setShutdown(true)`; keep reporting so redemptions process; depositors can cancel pending deposits |

## 6. Rollback

Contracts are not upgradeable. A replacement is a new deployment plus migration by users (exits always work).
