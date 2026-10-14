# MILORD

The next version of PMFI: a tradeable **MILORD** token on Robinhood Chain, 6,767 unique convertible
**Milords** NFTs, and protocol fees turned into **pARB** shares in a USDG vault on the same chain, distributed weekly to holders.

> **Unaudited. Local and test chains only.** Nothing here has been deployed to any public network.
> Nothing here promises a return.

| Folder | Contents |
|---|---|
| `src/robinhood` | MILORDToken, MilordsNFT, MilordConverter, RevenueSplitter, BuybackBurner, StrategyRegistry, MilordGovernor |
| `src/parb` | PMFIArbVaultV3 (pARB V2 fork, 0% fee, PPS fixes), ParbFundingAdapter, ShareDistributor |
| `src/legacy` | compile-only copy of the deployed V2 source, used to reproduce its issues |
| `test` | 139 Foundry unit, fuzz and invariant tests, including Robinhood deployment and the full fee-to-pARB flow |
| `script` | `MilordDeployer` (one-chain wiring), `DeployLocal` (anvil only), `DeployRobinhood` (testnet 46630 only, refuses mainnet) |
| `offchain` | quote signing, report signing, eligibility + cumulative Merkle builder, local quote service |
| `sim` | `run-local.sh` 3-week end-to-end simulation, dashboard builder, outputs |
| `frontend-integration` | browser SDK + app page in the prototype's visual style |
| `docs` | audit, architecture, tokenomics, fees, conversion, pARB, accounting, rewards, Hookr, security, open decisions, runbook |

Launch readiness (Hookr on Robinhood Chain): start with `docs/launch/README.md`. Background: `docs/OPEN_DECISIONS.md`, `docs/ROBINHOOD_VAULT_MIGRATION.md`, `docs/LOCAL_DEVELOPMENT.md`.
