# Local development and end-to-end environment

```bash
cd milord
npm install                      # OpenZeppelin, merkle-tree, ethers, forge-std
forge build
forge test                       # unit + fuzz + invariant (104 tests)
node offchain/test-eligibility.mjs
./sim/run-local.sh               # fresh anvil -> DeployLocal -> 3-week simulation -> sim/output/simulation.json
node sim/build-dashboard.mjs     # -> sim/output/dashboard.html
```

`script/DeployLocal.s.sol` refuses to run on any chain but 31337 and uses anvil's public test keys only.
Both chains are simulated on one anvil; the bridge and swap are mocks (`MockUSDC.mint`, `MockSwapAdapter`).

Frontend: `frontend-integration/` contains `milord-sdk.mjs` (contract calls) and `milords-app.html`
(the prototype's look, wired to a local deployment). Serve the folder and open it with
`?deployments=../sim/deployments.local.json` after running the simulation deploy.
