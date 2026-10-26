# Frontend integration

The Milords website exists only as compiled prototype artifacts (no source project in the repo), so this
folder delivers the integration as two drop-in pieces instead of a redesign:

- `milord-sdk.mjs`: every contract call the site needs (network switch to Robinhood Chain, market price and
  fee disclosure, status, wake, redeem, choose characteristics, strategy info, pARB balance and price, rewards,
  claim, governance votes). Framework-free ES module, ethers v6.
- `milords-app.html`: an app page using the prototype's tokens and components (sky/ink/butter palette, rows,
  ledger, chips) wired to the SDK. Use it as is, or move its four sections into the site once the source is available.

Run locally:

```bash
./sim/run-local.sh                    # anvil + deploy + simulation (creates deployments + epoch file)
node offchain/quote-server.mjs &      # local quote service (anvil only)
npx http-server . -p 8080             # from milord/
# open http://127.0.0.1:8080/frontend-integration/milords-app.html with MetaMask on 127.0.0.1:8545
```

Query parameters: `deployments` (addresses JSON), `epoch` (the active root's epoch file), `pending` (the pending root's epoch file),
`rewards` (the publisher's `rewards.json`), `quote` (quote service),
`hookr` (the MILORD pool page on hookr.fun, shown as "Trade on Hookr"), `market` (the collection's marketplace
URL; each Milord links to `<market>/<id>`). Without `hookr`/`market` those links are hidden rather than guessed.
Locally the page also shows Buy/Sell buttons against the local test pool; on Robinhood Chain users trade on Hookr.

Copy changes needed on the existing site are listed in `docs/CURRENT_PROJECT_AUDIT.md` §5.
The vault and distributor are on the same chain as the token and NFTs, so one wallet connection covers everything.
