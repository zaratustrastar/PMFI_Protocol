# MILORD

The next version of PMFI: a tradeable **MILORD** token on Robinhood Chain, 6,767 unique convertible
**Milords** NFTs, and protocol fees turned into **pARB** shares on Base distributed weekly to holders.

> **Unaudited. Local and test chains only.** Nothing here has been deployed to any public network.
> Nothing here promises a return.

| Folder | Contents |
|---|---|
| `src/robinhood` | MILORDToken, MilordsNFT, MilordConverter, RevenueSplitter, BuybackBurner, StrategyRegistry, BasePayoutRegistry, MilordGovernor |
| `src/base` | PMFIArbVaultV3 (pARB V2 fork, 0% fee, PPS fixes), ParbFundingAdapter, ShareDistributor |
| `src/legacy` | compile-only copy of the deployed V2 source, used to reproduce its issues |
| `test` | 104 Foundry unit, fuzz and invariant tests |
| `script/DeployLocal.s.sol` | anvil-only deployment of both sides |
| `offchain` | quote signing, report signing, eligibility + cumulative Merkle builder, local quote service |
| `sim` | `run-local.sh` 3-week end-to-end simulation, dashboard builder, outputs |
| `frontend-integration` | browser SDK + app page in the prototype's visual style |
| `docs` | audit, architecture, tokenomics, fees, conversion, pARB, accounting, rewards, Hookr, security, open decisions, runbook |

Start with `docs/OPEN_DECISIONS.md` and `docs/LOCAL_DEVELOPMENT.md`.
