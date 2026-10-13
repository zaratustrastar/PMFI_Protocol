# Local development and end-to-end environment

```bash
cd milord
npm install                      # OpenZeppelin, merkle-tree, ethers, forge-std
forge build
forge test                       # unit + fuzz + invariant (134 tests)
node offchain/test-eligibility.mjs
./sim/run-local.sh               # fresh anvil -> DeployLocal -> 3-week simulation -> sim/output/simulation.json
node sim/build-dashboard.mjs     # -> sim/output/dashboard.html
```

`script/DeployLocal.s.sol` refuses to run on any chain but 31337 and uses anvil's public test keys only.
The anvil chain stands in for Robinhood Chain; USDG and the ETH→USDG swap are mocks (`MockUSDG`, `MockSwapAdapter`).
`script/DeployRobinhood.s.sol` deploys the same set to Robinhood testnet 46630 from `MILORD_*` env vars and refuses
chain 4663. `offchain/gen_reporter_fixture.py` signs a report with the real `arb_reporter.py` for the compatibility test.

Frontend: `frontend-integration/` contains `milord-sdk.mjs` (contract calls) and `milords-app.html`
(the prototype's look, wired to a local deployment). Serve the folder and open it with
`?deployments=../sim/deployments.local.json` after running the simulation deploy.

## Hookr launch tooling (local)

```bash
./sim/run-local.sh                         # anvil + Permit2 + DeployLocal (protocol + local Hookr-style v4 market) + simulation
node sim/build-dashboard.mjs               # sim/output/dashboard.html
node offchain/launch-plan.mjs              # illustrative launch options -> sim/output/launch-options.json
node offchain/safe-launch-batch.mjs        # Safe Transaction Builder JSON for the MILORD-only position (deploy/out/)
forge script script/HookrPostLaunch.s.sol  # V4SwapAdapter + MilordLiquidityLock for a pool key (refuses chain 4663)
```
