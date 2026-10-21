#!/usr/bin/env bash
# Private anvil on 127.0.0.1:${ANVIL_PORT:-8545} -> Permit2 -> DeployLocal -> entry-fee price integration test.
# Local only; anvil test keys only. MILORD_PREBUILT=1 runs on prebuilt production artifacts without compiling.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f env.sh ] && . ./env.sh
. sim/anvil-lib.sh
start_anvil
deploy_local
node offchain/test-entry-price.mjs
