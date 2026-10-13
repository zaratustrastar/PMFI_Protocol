# Frontend integration

The Milords website exists only as compiled prototype artifacts (no source project in the repo), so this
folder delivers the integration as two drop-in pieces instead of a redesign:

- `milord-sdk.mjs`: every contract call the site needs (status, wake, redeem, choose characteristics,
  rewards, claim, Base payout address). Framework-free ES module, ethers v6.
- `milords-app.html`: an app page using the prototype's tokens and components (sky/ink/butter palette, rows,
  ledger, chips) wired to the SDK. Use it as is, or move its four sections into the site once the source is available.

Run locally:

```bash
./sim/run-local.sh                    # anvil + deploy + simulation (creates deployments + epoch file)
node offchain/quote-server.mjs &      # local quote service (anvil only)
npx http-server . -p 8080             # from milord/
# open http://127.0.0.1:8080/frontend-integration/milords-app.html with MetaMask on 127.0.0.1:8545
```

Copy changes needed on the existing site are listed in `docs/CURRENT_PROJECT_AUDIT.md` §5.
On mainnet the vault and distributor live on Base: the SDK comment marks where a Base provider is needed.
