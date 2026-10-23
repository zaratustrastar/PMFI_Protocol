# Low-memory machines (VPS) and CI artifacts

The production build (default profile: solc 0.8.26, via-IR, optimizer 200) peaks at about 5.7 GB of RAM.
On a small VPS, never run plain `forge build` / `forge test` / `forge script` without one of the two modes below.

## 1. Develop and test: `lowmem` profile

```
cd <repo>/milord
npm ci
FOUNDRY_PROFILE=lowmem forge build --skip script
FOUNDRY_PROFILE=lowmem forge test --skip script      # 179 tests (+4 fork tests skipped), ~700 MB peak
```

Output goes to `out-lowmem/` and `cache-lowmem/`, so it never touches the production `out/` and `cache/`.
Not for gas numbers or releases. `script/DeployLocal.s.sol` needs via-IR, hence `--skip script`.

## 2. Run the anvil simulations: production artifacts from CI

The `milord-ci` workflow uploads the production build as the artifact `milord-production-out` (`out/` and
`cache/`, kept 30 days). With those in place, Foundry finds nothing to compile.

```
cd <repo>/milord
git fetch origin <branch> && git checkout <sha>                 # milord/autonomy (staging) or milord/v1-protocol (PR #1); the exact commit CI built
npm ci                                                          # node_modules must match package-lock.json
RUN=$(gh run list -R zaratustrastar/PMFI_Protocol --workflow milord-ci --commit <sha> --status success \
      --json databaseId --jq '.[0].databaseId')
rm -rf out cache
gh run download "$RUN" -R zaratustrastar/PMFI_Protocol -n milord-production-out -D .
MILORD_PREBUILT=1 ./sim/run-local.sh                            # 46/46 checks
MILORD_PREBUILT=1 ./sim/run-entry-price.sh                      # 19 checks
```

Without `gh`: open the successful run for that commit in GitHub Actions, download `milord-production-out`,
and unzip it into `milord/` so that `milord/out` and `milord/cache` exist.

`MILORD_PREBUILT=1` replaces solc with `sim/no-compile-solc.sh`. If anything would need compiling (a different
commit, edited sources, a different `node_modules`), the run stops with "refusing to compile" instead of
starting a via-IR build. solc does not need to be installed on the VPS.

Foundry version: CI builds with Foundry 1.5.1. Verified with Foundry 1.7.1 using 1.5.1 artifacts: no
recompilation, both simulations passed (39/39 and 19 at the time). The guard above catches any future version that disagrees.

Anvil: each runner starts its own anvil on `127.0.0.1:${ANVIL_PORT:-8545}`, refuses to start if that port is
in use, and stops only its own process on exit (including Ctrl-C). Use `ANVIL_PORT=8546` to run next to
something else. Local test keys only; nothing is broadcast to a live network.

## 3. Engine and services tests (no chain, low memory)

```
python3 -m unittest discover -s bot/arb_monitor/tests      # from the repo root: 143 tests, 2 expected failures
cd milord && node --test offchain/services/test/*.test.mjs # 29 tests
```
