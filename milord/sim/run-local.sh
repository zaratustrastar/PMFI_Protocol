#!/usr/bin/env bash
# Private anvil on 127.0.0.1:${ANVIL_PORT:-8545} -> Permit2 at its canonical address -> deploy (protocol + local
# Hookr-style v4 market) -> end-to-end simulation. Local only; anvil public test keys only.
# MILORD_PREBUILT=1 runs on prebuilt production artifacts without compiling (see sim/anvil-lib.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f env.sh ] && . ./env.sh
. sim/anvil-lib.sh
start_anvil
deploy_local
node sim/simulate.mjs
