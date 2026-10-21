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
| 1 | Hookr launch parameters | [HOOKR_FINAL_CONFIG.md](HOOKR_FINAL_CONFIG.md) |
| 2 | Final launch liquidity (100%), balances, first-buy impact | [LAUNCH_LIQUIDITY.md](LAUNCH_LIQUIDITY.md) |
| 2b | Permanent lock and LP fee burn | [LP_LOCK.md](LP_LOCK.md) |
| 5b | Automated ETH → USDG → pARB | [PARB_AUTOMATION.md](PARB_AUTOMATION.md) |
| 5c | Keeper, epoch publisher, root checker | `offchain/services/README.md` |
| 5d | NFT entry-fee pricing (TWAP quote, bootstrap, trust assumptions) | [ENTRY_FEE_PRICING.md](ENTRY_FEE_PRICING.md) |
| 6b | Hookr-1 verification and incompatibilities | [HOOKR_VERIFICATION.md](HOOKR_VERIFICATION.md) |
| 10b | Safe configuration and permissions | [SAFE_CONFIGURATION.md](SAFE_CONFIGURATION.md) |
| 13 | Blocker matrix | [BLOCKERS.md](BLOCKERS.md) |
| 14 | Atomic MILORD deployment + Hookr launch, pre-emption | [ATOMIC_LAUNCH.md](ATOMIC_LAUNCH.md) |
| 15 | Temporary and permanent lock, missed-handover contingency, Option D split | [LOCK_PROCEDURE.md](LOCK_PROCEDURE.md) |
| 16 | Hookr-1 bytecode verification | [HOOKR_BYTECODE_VERIFICATION.md](HOOKR_BYTECODE_VERIFICATION.md) |
| 17 | Fork clock, ArbSys stub, what to validate on Robinhood | [CLOCK_AND_ARBSYS.md](CLOCK_AND_ARBSYS.md) |
| 18 | Security-audit scope | [AUDIT_SCOPE.md](AUDIT_SCOPE.md) |
| 19 | Robinhood fork suite | [FORK_INTEGRATION.md](FORK_INTEGRATION.md) |
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
