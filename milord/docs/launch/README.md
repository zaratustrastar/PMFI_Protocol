# MILORD V1 launch readiness (Hookr on Robinhood Chain)

Status tags used in every document here:

| Tag | Meaning |
|---|---|
| **TESTED** | implemented and covered by Foundry tests and/or the end-to-end simulation |
| **BUILT, NOT INTEGRATION-TESTED** | code exists and runs locally, not yet exercised against the real external system |
| **SIMULATED WITH MOCKS** | only exercised against a local stand-in (Hookr hook, USDG, P&L) |
| **EXTERNAL** | depends on Hookr, Paxos, a venue or a bridge; verified from public docs only |
| **NEEDS APPROVAL** | a value or choice the owner must approve before production |

| # | Deliverable | File |
|---|---|---|
| 1 | Proposed Hookr launch parameters | [HOOKR_FINAL_CONFIG.md](HOOKR_FINAL_CONFIG.md) |
| 2 | Supply and pool allocation options | [TOKEN_ALLOCATION.md](TOKEN_ALLOCATION.md), [LIQUIDITY_ALLOCATION_10ETH.md](LIQUIDITY_ALLOCATION_10ETH.md) |
| 3 | Fee paths and recipients | [FEE_ROUTING_VERIFICATION.md](FEE_ROUTING_VERIFICATION.md) |
| 4 | Full economic transaction trace | [MILORD_END_TO_END.md](MILORD_END_TO_END.md) |
| 5 | Venue funding and withdrawal routes | [ARBITRAGE_FUNDING_READINESS.md](ARBITRAGE_FUNDING_READINESS.md) |
| 6, 9 | Simulation and Foundry results | [SIMULATION_REPORT.md](SIMULATION_REPORT.md) |
| 7 | Dashboard | `sim/output/dashboard.html` (rebuild: `node sim/build-dashboard.mjs`) |
| 8 | Website integration | `frontend-integration/` |
| 10 | Security findings and risk register | [RISK_REGISTER.md](RISK_REGISTER.md) |
| 11 | Deployment order, rollback and incidents | [DEPLOYMENT_ORDER.md](DEPLOYMENT_ORDER.md) |
| 12 | Launch checklist | [MAINNET_LAUNCH_CHECKLIST.md](MAINNET_LAUNCH_CHECKLIST.md) |

Nothing has been deployed or broadcast to any public network. Contracts are unaudited. All simulated amounts
are simulated, not financial performance.
